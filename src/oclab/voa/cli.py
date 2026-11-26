"""oclab-voa: replay recorded received-power fading through the MEMS VOA.

    oclab-voa inspect datasets/2026-11-19_night
    oclab-voa prepare datasets/2026-11-19_night --session 20261119T200402Z --start 600 \\
        --duration 300 --join-bursts --out voa_runs/seg01.csv
    oclab-voa --vdd 4.97 calibrate --out voa_runs/voa_cal.csv
    oclab-voa --vdd 4.97 play voa_runs/seg01.csv --cal voa_runs/voa_cal.csv --dry-run
    oclab-voa --vdd 4.97 play voa_runs/seg01.csv --cal voa_runs/voa_cal.csv
    oclab-voa validate voa_runs/seg01-run-<time>.csv

The VOA is a bright type (more voltage, less light), so every sample goes
through the calibration; the recording is never sent to it directly.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

from . import analysis, board as board_module, drive, sources, trace as trace_module
from .. import __version__

DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_MEAN_DBM = -15.0
SAMPLE_RATE_HZ = trace_module.TARGET_RATE_HZ


def open_board(args) -> board_module.Board:
    return board_module.Board(args.port, drive.code_limit(args.vdd, args.vmax),
                              boot_wait_s=board_module.BOOT_WAIT_S)


def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# inspect, prepare
# --------------------------------------------------------------------------

def command_inspect(args, ask=input):
    sessions = sources.open_source(args.source)
    if args.session:
        sessions = [sources.find_session(args.source, args.session)]
    results = []
    for session in sessions:
        r = trace_module.survey(session, max_flagged=args.max_flagged)
        results.append(r)
        if args.json:
            continue
        print(f"\n{r['session_id']}  {r['started_utc'][:19]} to {r['ended_utc'][11:19]} UTC")
        print(f"  {r['bursts']} bursts of {r['samples_per_burst']} samples at {r['rate_hz']} Hz "
              f"({r['burst_span_s']:.4f} s each), started every {r['interval_median_s'] or 0:.3f} s (median)")
        print(f"  Gaps: {r['gaps']} (median {r['gap_median_ms']:.0f} ms, longest {r['gap_max_ms']:.0f} ms); "
              f"coverage {r['coverage']:.1%}; longest gap-free stretch {r['longest_gap_free_s']:.3f} s")
        print(f"  SUM mean {r['sum_mean_v']:.2f} V; flagged (saturated, rail or below threshold) {r['flagged_fraction']:.1%}; "
              f"any-channel clip flag {r['clipped_any_channel_fraction']:.1%}")
        if r["windows"]:
            print(f"  Longest runs of bursts with at most {args.max_flagged:.0%} flagged samples:")
            for w in r["windows"]:
                print(f"    {w['start_utc'][11:23]} UTC (--start {w['start_s']:.1f}): {w['bursts']} bursts, "
                      f"{w['joined_s']:.1f} s joined ({w['wall_clock_s']:.1f} s of wall clock), "
                      f"{w['flagged_fraction']:.2%} flagged")
        else:
            print("  No burst meets the flag limit: not suitable for playback.")
        for d in r["dark_candidates"]:
            print(f"  Possible dark (beam off or blocked): {d['start_utc'][11:19]} UTC, {d['bursts']} bursts, "
                  f"SUM {d['mean_v'] * 1e3:.1f} mV (within-burst std {d['within_burst_std_v'] * 1e3:.1f} mV)")
    if args.json:
        print(json.dumps(results, indent=2))
    elif any(r["gaps"] for r in results):
        print("\nThese recordings are burst-mode: the longest continuous stretch is one burst. "
              "prepare refuses gapped windows unless --join-bursts is given.")


def command_prepare(args, ask=input):
    session = sources.find_session(args.source, args.session)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    p = trace_module.prepare(session, out, start=args.start, duration_s=args.duration, dark_v=args.dark,
                             join_bursts=args.join_bursts)
    w, c, s, f = p["window"], p["continuity"], p["statistics"], p["flags"]
    print(f"Saved {w['samples']} samples ({w['duration_s']:.1f} s at 1 kHz) to {out}, provenance in {out.with_suffix('.json')}")
    print(f"Source {session.id}: {w['bursts']} bursts from {w['first_burst_started_utc'][11:23]} to "
          f"{w['last_burst_received_utc'][11:23]} UTC ({w['wall_clock_span_s']:.1f} s of wall clock)")
    r = p["resampling"]
    if r.get("taps"):
        print(f"Anti-alias: {r['rate_in_hz']} Hz to 1 kHz (x{r['up']}/{r['down']}), {r['taps']}-tap Kaiser FIR, "
              f"flat to {r['passband_hz']:.0f} Hz, {r['attenuation_db']:.0f} dB from {r['stopband_hz']:.0f} Hz")
        if r["interpolated_from_slower_source"]:
            print(f"Warning: the source is {r['rate_in_hz']} Hz, slower than 1 kHz. Interpolation cannot add "
                  "the missing fluctuations.")
    if c["bursts_joined"]:
        print(f"JOINED: {c['gaps']} gaps left out (median {c['gap_median_ms']:.0f} ms, {c['missing_s']:.1f} s in total). "
              "This trace is not a continuous record.")
    print(f"Dark {p['dark']['value_v']} V ({p['dark']['source']}); mean SUM above dark {p['normalisation']['mean_sum_minus_dark_v']:.4f} V")
    print(f"Scintillation index {s['scintillation_index']:.4f}; h from {s['h_min']:.3f} to {s['h_max']:.3f}")
    print(f"Flagged output samples {f['output_flagged_fraction']:.2%} (input: saturated or rail "
          f"{f['input_saturated_fraction']:.2%}, below {f['sum_threshold_v']} V {f['input_below_threshold_fraction']:.2%})")
    if f["output_flagged_fraction"] > 0.05:
        print("Warning: more than 5 % of the trace is flagged; saturation flattens the peaks of the fading.")


# --------------------------------------------------------------------------
# calibrate, set
# --------------------------------------------------------------------------

def command_calibrate(args, ask=input):
    if args.steps < drive.MIN_CALIBRATION_POINTS:
        raise ValueError(f"--steps must be at least {drive.MIN_CALIBRATION_POINTS}")
    voltages = np.linspace(0.0, args.vmax, args.steps)
    codes = drive.volts_to_codes(voltages, args.vdd, args.vmax)
    unit = "monitor_v" if args.monitor else "uW"
    rows = []
    with open_board(args) as board:
        board.set_code(0)
        time.sleep(args.settle)
        ask("Block the light into the photodetector (or switch the laser off), then press Enter.")
        dark_monitor = float(drive.adc_to_volts(board.query(8), args.vdd))
        print(f"Dark A0 monitor level {dark_monitor:.4f} V")
        dark_meter = 0.0
        if not args.monitor:
            text = ask("Power meter reading with the light blocked, in µW (Enter for 0): ").strip()
            dark_meter = float(text) if text else 0.0
        ask("Restore the light, then press Enter.")
        if not args.monitor:
            print("Type the power meter reading in µW at each step (linear, not dBm). Type q to stop early.")
        try:
            for code in codes:
                board.set_code(int(code))
                time.sleep(args.settle)
                applied = float(drive.codes_to_volts(code, args.vdd))
                a0 = board.query(8)
                monitor = float(drive.adc_to_volts(a0, args.vdd)) - dark_monitor
                if a0 >= drive.ADC_SATURATED:
                    print(f"{applied:5.2f} V: the A0 monitor is at the top of its range. Reduce the light or the "
                          "INTENSITY gain.")
                    if args.monitor:
                        raise SystemExit(1)
                if args.monitor:
                    power = monitor
                    print(f"{applied:5.2f} V  monitor {monitor:.4f} V above dark")
                else:
                    text = ask(f"{applied:5.2f} V  power (µW) = ").strip()
                    if text.lower() == "q":
                        break
                    power = float(text) - dark_meter
                rows.append({"voltage_v": applied, "code": int(code), "power": power, "monitor_v": monitor})
        finally:
            board.set_code(0)
    meta = {"unit": unit, "vdd": args.vdd, "vmax": args.vmax, "settle_s": args.settle, "port": args.port,
            "monitor_dark_v": dark_monitor, "meter_dark_uw": None if args.monitor else dark_meter,
            "tool": f"oclab.voa {__version__}"}
    drive.save_calibration(args.out, rows, meta)
    print(f"Saved {len(rows)} points to {args.out}")
    cal = drive.load_calibration(args.out)
    print(f"Usable range {cal.range_db:.1f} dB between {cal.voltage[0]:.2f} V and {cal.voltage[-1]:.2f} V"
          + (f"; {cal.dropped} points dropped (flat or at dark)" if cal.dropped else ""))
    if unit == "uW":
        print(f"0 V transmission {drive.uw_to_dbm(cal.power[0]):.1f} dBm, deepest {drive.uw_to_dbm(cal.power[-1]):.1f} dBm")


def command_set(args, ask=input):
    code = int(drive.volts_to_codes([args.volts], args.vdd, args.vmax)[0])
    with open_board(args) as board:
        board.set_code(code)
        time.sleep(0.3)
        a0 = board.query(8)
    print(f"Code {code}: output {float(drive.codes_to_volts(code, args.vdd)):.3f} V. "
          f"A0 monitor {a0:.1f} of 1023 = {float(drive.adc_to_volts(a0, args.vdd)):.4f} V"
          + (" (at the top of the range)" if a0 >= drive.ADC_SATURATED else ""))
    print("The output holds this value until the port is opened again (which resets the board to 0 V).")


# --------------------------------------------------------------------------
# play
# --------------------------------------------------------------------------

def load_trace(path):
    path = Path(path)
    with open(path, encoding="ascii") as file:
        header = file.readline().strip().split(",")
    if "h" not in header:
        raise ValueError(f"{path}: not a prepared trace (needs a header with an 'h' column); run 'prepare' first")
    data = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
    columns = {name: data[:, i] for i, name in enumerate(header)}
    if data.shape[0] == 0 or not np.all(np.isfinite(data)):
        raise ValueError(f"{path} is empty or has non-numeric samples")
    meta_path = path.with_suffix(".json")
    provenance = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None
    return columns, provenance


def write_log(path, codes, vdd, target, monitor):
    """One row per played sample; monitor_v is A0 read 1 ms after that sample was applied."""
    n = codes.size
    after = np.full(n, np.nan)
    clipped = np.zeros(n, dtype=int)
    usable = min(n, max(monitor.size - 1, 0))
    after[:usable] = drive.adc_to_volts(monitor[1:usable + 1], vdd)
    clipped[:usable] = monitor[1:usable + 1] >= drive.ADC_SATURATED
    drive_v = drive.codes_to_volts(codes, vdd)
    target = np.full(n, np.nan) if target is None else target
    with open(path, "w", encoding="ascii", newline="\n") as file:
        file.write("index,dac_code,drive_v,target_power,monitor_v,monitor_clipped\n")
        file.writelines(f"{i},{c},{d:.4f},{'' if np.isnan(t) else f'{t:.6g}'},{'' if np.isnan(m) else f'{m:.4f}'},{k}\n"
                        for i, (c, d, t, m, k) in enumerate(zip(codes.tolist(), drive_v, target, after, clipped)))
    return float(clipped.mean())


def command_play(args, ask=input):
    columns, provenance = load_trace(args.trace)
    h = columns["h"]
    n = h.size
    print(f"Trace {args.trace}: {n} samples, {n / SAMPLE_RATE_HZ:.1f} s at 1 kHz")
    if provenance:
        if provenance["continuity"]["bursts_joined"]:
            print(f"Note: bursts joined end to end ({provenance['continuity']['gaps']} gaps left out); "
                  "not a continuous record.")
        if "flagged" in columns and columns["flagged"].any():
            print(f"Note: {columns['flagged'].mean():.2%} of samples are flagged (saturated or weak SUM).")
    calibration, stats, target = None, None, None
    if args.direct:
        if "sum_v" not in columns:
            raise ValueError("--direct needs the sum_v column of a prepared trace")
        volts = columns["sum_v"]
        print("DIRECT (bench test): the recorded SUM volts are copied to the output. Not for a turbulence test.")
        print(f"Above the {args.vmax} V limit: {np.mean(volts > args.vmax):.2%}; below 0 V: {np.mean(volts < 0):.2%}")
        hold_v = 0.0 if args.hold is None else args.hold
    else:
        if not args.cal:
            raise ValueError("Refusing to play without a calibration: give --cal FILE "
                             "(or --direct for an electrical bench test)")
        calibration = drive.load_calibration(args.cal)
        mean_power = args.mean_power
        if args.mean_dbm is not None:
            if calibration.unit != "uW":
                raise ValueError("--mean-dbm needs a calibration in µW; use --mean-power with this calibration")
            mean_power = drive.dbm_to_uw(args.mean_dbm)
        elif mean_power is None and not args.fit_mean and calibration.unit == "uW":
            mean_power = drive.dbm_to_uw(DEFAULT_MEAN_DBM)
        volts, target, stats = drive.plan_drive(h, calibration, mean_power)
        hold_v = float(calibration.voltage_for(stats["mean_power"])) if args.hold is None else args.hold
        unit = "µW" if calibration.unit == "uW" else "V (monitor)"
        print(f"Calibration {args.cal}: {calibration.range_db:.1f} dB, playable {stats['power_min']:.4g} to "
              f"{stats['power_max']:.4g} {unit}")
        print(f"Mean power {stats['mean_power']:.4g} {unit}"
              + (f" ({stats['mean_dbm']:.1f} dBm)" if "mean_dbm" in stats else "")
              + f", {stats['below_0v_transmission_db']:.1f} dB below the 0 V transmission")
        print(f"Scintillation index: trace {stats['scintillation_index_trace']:.4f}, as played "
              f"{stats['scintillation_index_played']:.4f}")
        print(f"Clipped: {stats['clipped_high_fraction']:.2%} at the top (95 % of 0 V transmission), "
              f"{stats['clipped_low_fraction']:.2%} at the deepest calibrated point")
    codes = drive.volts_to_codes(volts, args.vdd, args.vmax)
    hold = drive.volts_to_codes([hold_v], args.vdd, args.vmax)
    limit = drive.code_limit(args.vdd, args.vmax)
    print(f"Drive {float(drive.codes_to_volts(codes.min(), args.vdd)):.3f} to "
          f"{float(drive.codes_to_volts(codes.max(), args.vdd)):.3f} V (limit {args.vmax} V = code {limit}); "
          f"hold afterwards {float(drive.codes_to_volts(hold[0], args.vdd)):.3f} V")
    if args.save_drive:
        np.savetxt(args.save_drive, codes, fmt="%d", header="dac_code", comments="")
        print(f"Saved the drive codes to {args.save_drive}")
    if args.dry_run:
        print("Dry run: nothing sent.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = Path(args.log) if args.log else Path(args.trace).with_name(f"{Path(args.trace).stem}-run-{stamp}.csv")
    sent = np.concatenate([codes, hold])
    with open_board(args) as board:
        board.set_code(int(sent[0]))
        time.sleep(0.05)
        report, monitor, started, ended, unexpected = board.play(sent)
    clipped_fraction = write_log(log, codes, args.vdd, target, monitor)
    problems = []
    if report["played"] != sent.size:
        problems.append(f"played {report['played']} of {sent.size} samples")
    for key in ("underruns", "monitor_dropped", "i2c_errors"):
        if report[key]:
            problems.append(f"{report[key]} {key.replace('_', ' ')}")
    if monitor.size != report["played"]:
        problems.append(f"received {monitor.size} monitor samples for {report['played']} played")
    details = {
        "tool": f"oclab.voa {__version__}", "started_utc": started.isoformat(timespec="milliseconds"),
        "ended_utc": ended.isoformat(timespec="milliseconds"),
        "time_note": "PC clock when 'G' was sent; the D8 rising edge follows within about a millisecond plus USB latency",
        "accepted": not problems, "problems": problems, "board_report": report, "monitor_samples": int(monitor.size),
        "unexpected_bytes": unexpected, "monitor_clipped_fraction": clipped_fraction,
        "settings": {"port": args.port, "vdd": args.vdd, "vmax": args.vmax, "code_limit": limit, "direct": args.direct,
                     "sample_rate_hz": SAMPLE_RATE_HZ, "hold_v": hold_v, "hold_code": int(hold[0])},
        "drive": stats,
        "trace": {"path": str(args.trace), "sha256": sha256(args.trace), "provenance": provenance},
        "calibration": None if calibration is None else {
            "path": str(args.cal), "sha256": sha256(args.cal), "meta": calibration.meta, "unit": calibration.unit,
            "voltage_v": calibration.voltage.tolist(), "power": calibration.power.tolist()},
        "log": log.name,
    }
    log.with_suffix(".json").write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")
    print(f"Started {details['started_utc']}. Played {report['played']} of {sent.size}, underruns {report['underruns']}, "
          f"maximum lateness {report['max_late_us']} µs, monitor samples {monitor.size} "
          f"(dropped {report['monitor_dropped']})")
    print(f"Log {log} and {log.with_suffix('.json')}")
    if clipped_fraction:
        print(f"Warning: {clipped_fraction:.2%} of monitor samples were at the top of the A0 range.")
    if problems:
        raise SystemExit("REJECTED: " + "; ".join(problems) + ". Discard this run.")


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------

def command_validate(args, ask=input):
    log = np.genfromtxt(args.log, delimiter=",", names=True)
    meta_path = Path(args.log).with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if meta and not meta.get("accepted", True):
        print("Warning: this run was rejected at playback (" + "; ".join(meta["problems"]) + ").")
    target = log["target_power"]
    if not np.isfinite(target).any():
        raise ValueError("The log has no target power (a --direct run); nothing to validate against")
    dark = args.monitor_dark
    if dark is None:
        dark = ((meta.get("calibration") or {}).get("meta") or {}).get("monitor_dark_v")
        if dark is None:
            dark = 0.0
            print("Warning: no monitor dark level in the run's calibration; assuming 0 V (use --monitor-dark).")
    skip = int(round(args.skip * SAMPLE_RATE_HZ))
    monitor = log["monitor_v"] - dark
    good = np.isfinite(target) & np.isfinite(monitor) & (log["monitor_clipped"] == 0)
    good[:skip] = False
    excluded = 1 - good[skip:].mean()
    # Spectra need a contiguous series: use the longest run of good samples.
    runs = trace_module.true_runs(good)
    first, length = runs[0] if runs else (0, 0)
    if length < 1024:
        raise ValueError("Fewer than 1024 contiguous usable samples in the log")
    part = slice(first, first + length)
    r = analysis.compare(target[part], monitor[part])
    print(f"{args.log}: {r['samples']} samples compared ({excluded:.2%} excluded as clipped or missing), "
          f"monitor dark {dark:.4f} V, lag {r['lag_samples']} ms, correlation {r['correlation']:.3f}")
    print("Both series normalised to their own means (INTENSITY may not hold the mean level).")
    si = r["scintillation_index"]
    print(f"Scintillation index: target {si['target']:.4f}, monitor {si['monitor']:.4f}")
    print("Quantiles of h      " + "  ".join(f"{q:>6.0%}" for q in r["quantiles"]["q"]))
    for name in ("target", "monitor"):
        print(f"  {name:<18}" + "  ".join(f"{v:6.3f}" for v in r["quantiles"][name]))
    print(f"PDF: Kolmogorov-Smirnov distance {r['ks_distance']:.3f}")
    print("Variance by band       target     monitor   monitor/target")
    for b in r["bands"]:
        ratio = "" if b["ratio_db"] is None else f"{b['ratio_db']:+.1f} dB"
        print(f"  {b['band_hz'][0]:>5g}-{b['band_hz'][1]:<5g} Hz {b['target']:10.3g} {b['monitor']:10.3g}   {ratio}")
    print("Fades              P(below)  per second  mean length (ms)   target / monitor")
    for t, m in zip(r["fades"]["target"], r["fades"]["monitor"]):
        print(f"  {t['threshold_db']:>5.0f} dB      {t['probability']:.4f}/{m['probability']:.4f}  "
              f"{t['fades_per_s']:5.2f}/{m['fades_per_s']:5.2f}   {t['mean_duration_ms']:6.1f}/{m['mean_duration_ms']:6.1f}")
    e = r["error_db"]
    print(f"Error (monitor / target): mean {e['mean']:+.2f} dB, rms {e['rms']:.2f} dB, 95 % within {e['p95_abs']:.2f} dB")
    if args.out:
        Path(args.out).write_text(json.dumps({"log": str(args.log), "monitor_dark_v": dark, "skip_s": args.skip,
                                              "excluded_fraction": float(excluded), **r}, indent=2) + "\n",
                                  encoding="utf-8")
        print(f"Saved the comparison, spectra and PDFs to {args.out}")


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oclab-voa", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"Arduino serial port (default {DEFAULT_PORT})")
    parser.add_argument("--vdd", type=float, default=5.0, help="measured voltage of the Arduino 5V pin (default 5.0)")
    parser.add_argument("--vmax", type=float, default=drive.MAX_DRIVE_V,
                        help=f"software drive limit in volts, at most {drive.MAX_DRIVE_V} (default {drive.MAX_DRIVE_V})")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("inspect", help="list sessions and windows suitable for playback")
    p.add_argument("source", help="dataset folder, .h5 file, data directory or recordings.sqlite3")
    p.add_argument("--session", help="only this session (ID or unique prefix)")
    p.add_argument("--max-flagged", type=float, default=0.01,
                   help="largest flagged fraction for a burst to count as usable (default 0.01)")
    p.add_argument("--json", action="store_true", help="print the survey as JSON")
    p.set_defaults(run=command_inspect)

    p = commands.add_parser("prepare", help="turn a session and window into a 1 kHz trace with provenance")
    p.add_argument("source", help="dataset folder, .h5 file, data directory or recordings.sqlite3")
    p.add_argument("--session", help="session ID or unique prefix (needed when the source holds several)")
    p.add_argument("--start", help="seconds after the session's first burst, or a UTC time such as 2026-11-19T20:30:00Z")
    p.add_argument("--duration", type=float, help="seconds of 1 kHz output (default: to the end of the session)")
    p.add_argument("--dark", type=float, help="SUM with the beam blocked, in volts (default 0, recorded as assumed)")
    p.add_argument("--join-bursts", action="store_true",
                   help="join separate bursts end to end as if continuous (recorded in the provenance)")
    p.add_argument("--out", required=True, help="trace CSV; the provenance is written beside it as .json")
    p.set_defaults(run=command_prepare)

    p = commands.add_parser("calibrate", help="step the drive and record transmitted power")
    p.add_argument("--out", default="voa_cal.csv")
    p.add_argument("--steps", type=int, default=26, help="voltage steps from 0 V to --vmax (default 26)")
    p.add_argument("--settle", type=float, default=0.5, help="seconds to wait at each step (default 0.5)")
    p.add_argument("--monitor", action="store_true",
                   help="read the A0 photodetector monitor instead of typing power meter readings")
    p.set_defaults(run=command_calibrate)

    p = commands.add_parser("set", help="hold one voltage and print the A0 monitor reading")
    p.add_argument("volts", type=float)
    p.set_defaults(run=command_set)

    p = commands.add_parser("play", help="play a prepared trace at 1 kHz through the VOA")
    p.add_argument("trace", help="trace CSV from 'prepare'")
    p.add_argument("--cal", help="calibration CSV from 'calibrate' (required unless --direct)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--mean-dbm", type=float,
                       help=f"mean received power in dBm (µW calibrations; default {DEFAULT_MEAN_DBM})")
    group.add_argument("--mean-power", type=float, help="mean power in the calibration's unit")
    group.add_argument("--fit-mean", action="store_true", help="use the largest mean that keeps every peak in range")
    p.add_argument("--hold", type=float, help="volts to hold after playback (default: the mean-power level; 0 V with --direct)")
    p.add_argument("--direct", action="store_true",
                   help="electrical bench test: copy the recorded SUM volts straight to the output")
    p.add_argument("--dry-run", action="store_true", help="convert and report only; nothing is sent")
    p.add_argument("--log", help="per-sample log CSV (default: <trace>-run-<UTC time>.csv)")
    p.add_argument("--save-drive", help="also save the drive codes")
    p.set_defaults(run=command_play)

    p = commands.add_parser("validate", help="compare the target with the logged monitor")
    p.add_argument("log", help="log CSV from 'play'")
    p.add_argument("--monitor-dark", type=float, help="A0 monitor volts with the light blocked (default: from the calibration)")
    p.add_argument("--skip", type=float, default=0.1, help="seconds to ignore at the start (default 0.1)")
    p.add_argument("--out", help="save the full comparison, spectra and PDFs as JSON")
    p.set_defaults(run=command_validate)
    return parser


def main(argv=None, ask=input):
    args = build_parser().parse_args(argv)
    try:
        drive.check_supply(args.vdd, args.vmax)
        args.run(args, ask=ask)
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        sys.exit(f"Error: {error}")


if __name__ == "__main__":
    main()
