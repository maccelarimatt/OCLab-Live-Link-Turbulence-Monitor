#!/usr/bin/env python3
"""Prepare recordings, calibrate the MEMS VOA and play back turbulence (version 2).

Install:   pip install numpy pyserial
Sketch:    voa_dac_streamer.ino version 2 (records the photodetector monitor on A0)

Typical use:
  1. Turn a recording into a 1 kHz trace (here 10 minutes from 2 hours in):
       python3 voa_playback.py prepare sum_1khz.csv --rate 1000 --start 7200 --duration 600 --out seg01.csv
  2. Calibrate, either automatically from the photodetector monitor on A0:
       python3 voa_playback.py --vdd 4.97 calibrate --monitor --port /dev/ttyUSB0 --out voa_cal.csv
     or by typing power meter readings:
       python3 voa_playback.py --vdd 4.97 calibrate --port /dev/ttyUSB0 --out voa_cal.csv
  3. Check, then play, saving the drive and monitor log:
       python3 voa_playback.py --vdd 4.97 play seg01.csv --cal voa_cal.csv --dark 0.012 --dry-run
       python3 voa_playback.py --vdd 4.97 play seg01.csv --cal voa_cal.csv --dark 0.012 \\
           --port /dev/ttyUSB0 --log seg01_run1.csv

Bench check without the VOA (the output copies the trace in volts):
  python3 voa_playback.py --vdd 4.97 play playback_test_trace.csv --direct --port /dev/ttyUSB0

A bright VOA gives LESS light at HIGHER voltage, so in normal use each sample is
converted to a drive voltage through the calibration file, never sent directly.
"""

import argparse
import json
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SAMPLE_RATE_HZ = 1000
CHUNK = 64
PREFILL_CHUNKS = 7          # 448 samples, inside the 512-sample Arduino buffer
DAC_MAX_CODE = 4095
ADC_FULL_SCALE = 1024
ADC_SATURATED = 1015        # Monitor codes at or above this are treated as clipped
USABLE_TRANSMISSION = 0.95  # Avoid the flat region near 0 V
BOOT_WAIT_S = 2.5           # Bootloader time after the port opens
READY_TIMEOUT_S = 4.0


# ----------------------------------------------------------------------------
# Traces and calibration
# ----------------------------------------------------------------------------

def load_column(path, column, skip_header):
    try:
        data = np.loadtxt(path, delimiter=",", usecols=(column,), skiprows=skip_header,
                          ndmin=1, dtype=float)
    except ValueError as error:
        raise ValueError(f"{path}: could not read column {column} as numbers ({error}). "
                         "Check --column and --skip-header.") from None
    if data.size == 0:
        raise ValueError(f"{path} contains no samples")
    if not np.all(np.isfinite(data)):
        raise ValueError(f"{path} has non-numeric samples in column {column}")
    return data


def load_calibration(path):
    """Return voltage and monotonic transmitted power, sorted by voltage.

    Points at the high-attenuation end with power at or below zero (monitor
    readings at the dark level) are dropped, because they cannot be inverted.
    """
    data = np.genfromtxt(path, delimiter=",", names=True, dtype=float)
    voltage = np.atleast_1d(data["voltage_v"])
    power = np.atleast_1d(data["power"])
    order = np.argsort(voltage)
    voltage, power = voltage[order], power[order]

    positive = power > 0
    if not positive[0]:
        raise ValueError("The calibration power at the lowest voltage is not positive. "
                         "Powers must be linear (uW, mW or monitor volts), not dBm.")
    keep = np.cumprod(positive).astype(bool)
    if keep.sum() < voltage.size:
        print(f"Calibration: using 0 to {voltage[keep][-1]:.2f} V. Higher voltages read at "
              "or below the dark level.")
    voltage, power = voltage[keep], power[keep]
    if voltage.size < 5:
        raise ValueError("The calibration needs at least 5 usable points")
    # Measurement noise must not make the inverse curve multi-valued.
    return voltage, np.minimum.accumulate(power)


def power_to_voltage(target, voltage, power):
    """Invert the falling curve, interpolating log power against voltage."""
    log_power = np.log10(power)
    unique_log, index = np.unique(log_power, return_index=True)
    return np.interp(np.log10(target), unique_log, voltage[index])


def build_drive(trace, calibration, dark, mean_power):
    """Return drive voltage, the applied target power and clipping statistics."""
    signal = trace - dark
    average = signal.mean()
    if average <= 0:
        raise ValueError("The mean recording is not above the dark level")
    fading = np.maximum(signal / average, 1e-4)

    voltage, power = calibration
    power_max = USABLE_TRANSMISSION * power[0]
    power_min = power[-1]
    if mean_power is None:
        mean_power = power_max / fading.max()   # Largest mean that fits the surge

    target = mean_power * fading
    stats = {
        "mean_power": mean_power,
        "power_max": power_max,
        "power_min": power_min,
        "clipped_high": float(np.mean(target > power_max)),
        "clipped_low": float(np.mean(target < power_min)),
        "scintillation_index": float(fading.var() / fading.mean() ** 2),
    }
    target = np.clip(target, power_min, power_max)
    return power_to_voltage(target, voltage, power), target, stats


def volts_to_codes(volts, vdd, vmax):
    limited = np.clip(volts, 0.0, min(vmax, vdd))
    codes = np.rint(limited / vdd * 4096.0)
    return np.clip(codes, 0, DAC_MAX_CODE).astype(np.uint16)


def adc_to_volts(codes, vdd):
    return np.asarray(codes, dtype=float) * vdd / ADC_FULL_SCALE


# ----------------------------------------------------------------------------
# Serial link
# ----------------------------------------------------------------------------

class Streamer:
    def __init__(self, port):
        import serial
        self.link = serial.Serial(port, 115200, timeout=0.05)
        self._wait_for_board()

    def close(self):
        self.link.close()

    def _wait_for_board(self):
        received = bytearray()

        # Opening the port resets most Uno and Nano boards. Stay silent while
        # the bootloader runs, because bytes sent to it can delay the sketch.
        boot_end = time.monotonic() + BOOT_WAIT_S
        while time.monotonic() < boot_end:
            received += self.link.read(64)

        deadline = time.monotonic() + READY_TIMEOUT_S
        next_reset = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_reset:
                self.link.write(b"R")
                next_reset = time.monotonic() + 0.5
            received += self.link.read(64)

            other = bytes(b for b in received if b not in b"KE")
            if other:
                text = other.decode("ascii", "replace").strip()
                raise RuntimeError("The board is running a different sketch. It sent "
                                   f"{text[:60]!r}. Upload voa_dac_streamer.ino (version 2).")
            if b"E" in received:
                raise RuntimeError("The sketch is set to use the MCP4725 and cannot find it. "
                                   "Set OUTPUT_PWM to 1 for the PWM circuit, then upload again.")
            if b"K" in received:
                time.sleep(0.05)
                self.link.reset_input_buffer()
                return

        raise TimeoutError(f"No reply from the Arduino after {BOOT_WAIT_S + READY_TIMEOUT_S:.0f} s. "
                           "Check that voa_dac_streamer.ino is uploaded and the Serial Monitor is closed.")

    def set_code(self, code):
        self.link.write(b"V" + struct.pack("<H", int(code)))
        self.link.flush()

    def query(self, repeats=4):
        """Mean A0 code over 16 x repeats conversions, only when not playing."""
        total = 0
        for _ in range(repeats):
            self.link.write(b"Q")
            self.link.flush()
            self.link.timeout = 1.0
            reply = self.link.read(3)
            self.link.timeout = 0.05
            if len(reply) != 3 or reply[0:1] != b"Q" or reply[1] < 0x80:
                raise RuntimeError(f"Unexpected monitor reply {reply!r}. Upload version 2 of the sketch.")
            total += ((reply[1] & 0x7F) << 7) | reply[2]
        return total / (16.0 * repeats)

    def _send_chunk(self, codes):
        self.link.write(b"D" + bytes([len(codes)]) + codes.astype("<u2").tobytes())

    def play(self, codes):
        """Play codes at 1 kHz. Return the board report and the A0 samples."""
        position = 0
        for _ in range(PREFILL_CHUNKS):
            chunk = codes[position:position + CHUNK]
            if chunk.size:
                self._send_chunk(chunk)
                position += chunk.size
        self.link.write(b"G")
        self.link.flush()
        self.started_utc = datetime.now(timezone.utc)

        monitor = []
        pending_high = None
        finish_sent = False
        last_progress = time.monotonic()
        while True:
            if position >= codes.size and not finish_sent:
                self.link.write(b"F")
                self.link.flush()
                finish_sent = True

            data = self.link.read(256)
            if not data:
                if time.monotonic() - last_progress > 3.0:
                    raise TimeoutError("Playback stalled. The output holds its last value.")
                continue
            last_progress = time.monotonic()

            index = 0
            while index < len(data):
                byte = data[index]
                index += 1
                if pending_high is not None:
                    monitor.append(((pending_high & 0x7F) << 7) | byte)
                    pending_high = None
                elif byte >= 0x80:
                    pending_high = byte
                elif byte == ord("C"):
                    chunk = codes[position:position + CHUNK]
                    if chunk.size:
                        self._send_chunk(chunk)
                        position += chunk.size
                elif byte == ord("Z"):
                    line = bytes(data[index:])
                    self.link.timeout = 1.0
                    if not line.endswith(b"\n"):
                        line += self.link.readline()
                    self.link.timeout = 0.05
                    fields = line.decode("ascii").split()
                    names = ("played", "underruns", "i2c_errors", "max_late_us", "monitor_dropped")
                    report = dict(zip(names, map(int, fields)))
                    return report, np.array(monitor, dtype=np.uint16)


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

def command_set(args):
    code = volts_to_codes(np.array([args.volts]), args.vdd, args.vmax)[0]
    streamer = Streamer(args.port)
    try:
        streamer.set_code(code)
        time.sleep(0.2)
        reading = streamer.query()
    finally:
        streamer.close()
    print(f"Code {code}, nominal {code * args.vdd / 4096:.3f} V. "
          f"Monitor on A0: {adc_to_volts(reading, args.vdd):.3f} V")


def command_calibrate(args):
    streamer = Streamer(args.port)
    voltages = np.linspace(0.0, args.vmax, args.steps)
    rows = []
    try:
        dark = 0.0
        if args.monitor:
            input("Block the light into the photodetector (or switch the laser off), "
                  "then press Enter.")
            streamer.set_code(0)
            time.sleep(args.settle)
            dark = adc_to_volts(streamer.query(8), args.vdd)
            print(f"Dark monitor level {dark:.4f} V")
            input("Restore the light, then press Enter.")
        else:
            print("Type the linear power reading at each step, in one unit (for example uW).")
            print("Do not use dBm. Type q to stop early and save the points so far.\n")

        for volts in voltages:
            code = volts_to_codes(np.array([volts]), args.vdd, args.vmax)[0]
            streamer.set_code(code)
            time.sleep(args.settle)
            applied = code * args.vdd / 4096
            if args.monitor:
                mean_code = streamer.query(8)
                if mean_code >= ADC_SATURATED:
                    print(f"{applied:5.2f} V: the monitor is at the top of the A0 range. "
                          "Reduce the light or the INTENSITY gain (RV1) and calibrate again.")
                    raise SystemExit(1)
                level = adc_to_volts(mean_code, args.vdd) - dark
                print(f"{applied:5.2f} V, monitor {level:.4f} V above dark")
                rows.append((applied, level))
            else:
                text = input(f"{applied:5.2f} V, power = ").strip()
                if text.lower() == "q":
                    break
                rows.append((applied, float(text)))
    finally:
        streamer.set_code(0)
        streamer.close()

    with open(args.out, "w", encoding="ascii") as file:
        file.write("voltage_v,power\n")
        for row in rows:
            file.write(f"{row[0]:.5f},{row[1]:.8g}\n")
    print(f"Saved {len(rows)} points to {args.out}")
    if rows:
        first, last = rows[0][1], rows[-1][1]
        if first > 0 and last > 0:
            print(f"Range: {10 * np.log10(first / last):.1f} dB between "
                  f"{rows[0][0]:.2f} V and {rows[-1][0]:.2f} V")


def command_play(args):
    trace = load_column(args.recording, args.column, args.skip_header)
    duration = trace.size / SAMPLE_RATE_HZ
    target = None

    if args.direct:
        drive = trace
        print("Direct mode: the recording is copied to the output in volts. "
              "Do not use this for a turbulence test.")
    else:
        if not args.cal:
            sys.exit("Use --cal voa_cal.csv, or --direct for an electrical bench check.")
        calibration = load_calibration(args.cal)
        drive, target, stats = build_drive(trace, calibration, args.dark, args.mean_power)
        extra_loss = 10 * np.log10(calibration[1][0] / stats["mean_power"])
        print(f"Samples {trace.size}, duration {duration:.1f} s")
        print(f"Scintillation index of the recording {stats['scintillation_index']:.4f}")
        print(f"Mean power {stats['mean_power']:.4g}, {extra_loss:.1f} dB below the 0 V level")
        print(f"Clipped at the surge limit {stats['clipped_high'] * 100:.2f}%, "
              f"at the fade limit {stats['clipped_low'] * 100:.2f}%")

    codes = volts_to_codes(drive, args.vdd, args.vmax)
    print(f"Drive range {codes.min() * args.vdd / 4096:.3f} to "
          f"{codes.max() * args.vdd / 4096:.3f} V")

    if args.save_drive:
        np.savetxt(args.save_drive, codes, fmt="%d", header="dac_code", comments="")
        print(f"Saved the drive codes to {args.save_drive}")

    if args.dry_run:
        return
    if not args.port:
        sys.exit("Use --port, or --dry-run to check the conversion only.")

    hold = volts_to_codes(np.array([args.hold]), args.vdd, args.vmax) if args.hold is not None \
        else codes[-1:]
    sent = np.concatenate([codes, hold])

    streamer = Streamer(args.port)
    try:
        streamer.set_code(sent[0])
        time.sleep(0.05)
        report, monitor = streamer.play(sent)
    finally:
        streamer.close()

    started = streamer.started_utc.isoformat(timespec="milliseconds")
    print(f"Started {started} (PC clock, about 1 ms before the D8 edge)")
    print(f"Played {report['played']} of {sent.size}, underruns {report['underruns']}, "
          f"I2C errors {report['i2c_errors']}, maximum lateness {report['max_late_us']} us, "
          f"monitor samples {monitor.size}")

    if args.log:
        write_log(args.log, codes, target, monitor, args.vdd)
        details = {
            "started_utc": started,
            "trace": str(args.recording),
            "calibration": args.cal,
            "direct": args.direct,
            "dark": args.dark,
            "mean_power": None if args.direct else float(stats["mean_power"]),
            "vdd": args.vdd,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "report": report,
        }
        with open(Path(args.log).with_suffix(".json"), "w", encoding="ascii") as file:
            json.dump(details, file, indent=2)

    clean = (report["played"] == sent.size and not report["underruns"]
             and not report["i2c_errors"] and not report["monitor_dropped"])
    if not clean:
        sys.exit("Playback was not clean. Discard this run.")


def write_log(path, codes, target, monitor, vdd):
    """One row per played sample. monitor_v is A0 read 1 ms after that sample was applied."""
    n = codes.size
    after = np.full(n, np.nan)
    usable = min(n, max(monitor.size - 1, 0))
    after[:usable] = adc_to_volts(monitor[1:usable + 1], vdd)
    clipped = np.zeros(n, dtype=int)
    clipped[:usable] = monitor[1:usable + 1] >= ADC_SATURATED
    columns = [np.arange(n), codes, codes * vdd / 4096]
    header = "index,dac_code,drive_v"
    if target is not None:
        columns.append(target)
        header += ",target_power"
    columns += [after, clipped]
    header += ",monitor_v,monitor_clipped"
    np.savetxt(path, np.column_stack(columns), delimiter=",", header=header, comments="",
               fmt=["%d", "%d", "%.4f"] + (["%.6g"] if target is not None else []) + ["%.4f", "%d"])
    print(f"Saved the drive and monitor log to {path}")
    if clipped.any():
        print(f"Warning: {clipped.mean() * 100:.2f}% of monitor samples were at the top of the A0 range.")


def longest_clean_stretch(t, gap_mask):
    """Start time and length of the longest run without a gap."""
    edges = np.flatnonzero(gap_mask)               # gap after sample i
    starts = np.concatenate([[0], edges + 1])
    ends = np.concatenate([edges, [t.size - 1]])
    lengths = t[ends] - t[starts]
    best = int(np.argmax(lengths))
    return t[starts[best]], lengths[best]


def command_prepare(args):
    values = load_column(args.recording, args.column, args.skip_header)

    if args.time_column is not None:
        t = load_column(args.recording, args.time_column, args.skip_header)
        if t.size != values.size:
            raise ValueError("The time and value columns have different lengths")
        if np.any(np.diff(t) <= 0):
            raise ValueError("The time column must increase steadily. Mixed bursts cannot be joined.")
        t = t - t[0]
        dt = np.diff(t)
        step = float(np.median(dt))
        rate = 1.0 / step
        limit = max(1.5 * step, args.max_gap_ms / 1000.0)
        gaps = dt > limit
    else:
        if not args.rate:
            sys.exit("Give --rate for a single-column recording, or --time-column.")
        rate = args.rate
        t = np.arange(values.size) / rate
        gaps = np.zeros(max(values.size - 1, 0), dtype=bool)

    start = args.start or 0.0
    stop = t[-1] if args.duration is None else start + args.duration
    if start >= t[-1] or stop > t[-1] + 1.0 / rate:
        raise ValueError(f"The recording lasts {t[-1]:.1f} s. Choose --start and --duration inside it.")

    window = (t >= start) & (t <= stop)
    inside = window[:-1] & window[1:]
    if np.any(gaps & inside):
        where = t[:-1][gaps & inside][:5]
        best_start, best_length = longest_clean_stretch(t, gaps)
        raise ValueError(f"{int(np.sum(gaps & inside))} gaps inside the chosen window, the first at "
                         f"{', '.join(f'{w:.3f}' for w in where)} s. The longest gap-free stretch "
                         f"starts at {best_start:.3f} s and lasts {best_length:.3f} s.")

    t_sel, v_sel = t[window], values[window]

    # Anti-aliasing: average over one output period when the source is faster.
    factor = int(round(rate / SAMPLE_RATE_HZ))
    if factor > 1:
        kernel = np.ones(factor) / factor
        padded = np.pad(v_sel, (factor // 2, factor - 1 - factor // 2), mode="edge")
        v_sel = np.convolve(padded, kernel, mode="valid")
        print(f"Source {rate:.0f} Hz: averaged over {factor} samples before resampling.")
    elif rate < SAMPLE_RATE_HZ * 0.999:
        print(f"Warning: the source is {rate:.0f} Hz, slower than 1 kHz. "
              "Interpolation cannot add the missing fast fluctuations.")

    grid = np.arange(t_sel[0], t_sel[-1] + 1e-9, 1.0 / SAMPLE_RATE_HZ)
    out = np.interp(grid, t_sel, v_sel)

    np.savetxt(args.out, out, fmt="%.6g")
    mean = out.mean()
    print(f"Saved {out.size} samples ({out.size / SAMPLE_RATE_HZ:.1f} s) to {args.out}")
    print(f"Mean {mean:.4g}, minimum {out.min():.4g}, maximum {out.max():.4g}")
    if args.dark is not None:
        signal = out - args.dark
        print(f"Scintillation index {signal.var() / signal.mean() ** 2:.4f} (dark {args.dark})")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vdd", type=float, default=5.0,
                        help="measured Arduino 5V pin voltage (default 5.0)")
    parser.add_argument("--vmax", type=float, default=5.0,
                        help="software drive limit in volts (default 5.0)")
    commands = parser.add_subparsers(dest="command", required=True)

    prep = commands.add_parser("prepare", help="turn a recording into a 1 kHz trace")
    prep.add_argument("recording", type=Path)
    prep.add_argument("--out", required=True)
    prep.add_argument("--column", type=int, default=0, help="value column (default 0)")
    prep.add_argument("--time-column", type=int, help="time column in seconds, if present")
    prep.add_argument("--rate", type=float, help="sample rate in Hz for a single-column file")
    prep.add_argument("--skip-header", type=int, default=0)
    prep.add_argument("--start", type=float, help="start time in seconds")
    prep.add_argument("--duration", type=float, help="length in seconds")
    prep.add_argument("--max-gap-ms", type=float, default=2.0,
                      help="largest allowed spacing between samples (default 2 ms)")
    prep.add_argument("--dark", type=float, help="dark level, to report the scintillation index")
    prep.set_defaults(run=command_prepare)

    set_parser = commands.add_parser("set", help="hold one voltage and read the monitor")
    set_parser.add_argument("volts", type=float)
    set_parser.add_argument("--port", required=True)
    set_parser.set_defaults(run=command_set)

    cal = commands.add_parser("calibrate", help="step the drive and record power")
    cal.add_argument("--port", required=True)
    cal.add_argument("--out", default="voa_cal.csv")
    cal.add_argument("--steps", type=int, default=26)
    cal.add_argument("--settle", type=float, default=0.5)
    cal.add_argument("--monitor", action="store_true",
                     help="read the photodetector monitor on A0 instead of typing values")
    cal.set_defaults(run=command_calibrate)

    play = commands.add_parser("play", help="play a 1 kHz trace")
    play.add_argument("recording", type=Path)
    play.add_argument("--cal")
    play.add_argument("--port")
    play.add_argument("--column", type=int, default=0)
    play.add_argument("--skip-header", type=int, default=0)
    play.add_argument("--dark", type=float, default=0.0,
                      help="recorded value with the beam blocked")
    play.add_argument("--mean-power", type=float,
                      help="mean power in calibration units (default: largest that fits)")
    play.add_argument("--hold", type=float, help="volts to hold after playback")
    play.add_argument("--direct", action="store_true",
                      help="bench check only: copy the trace to the output in volts")
    play.add_argument("--log", help="save drive, target and monitor for every sample")
    play.add_argument("--save-drive")
    play.add_argument("--dry-run", action="store_true")
    play.set_defaults(run=command_play)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        args.run(args)
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        sys.exit(f"Error: {error}")


if __name__ == "__main__":
    main()
