"""Turn a recorded SUM series into a normalised 1 kHz fading trace for the VOA.

    h(t) = (SUM(t) - dark) / mean(SUM - dark)   over the chosen window

Faster sources are low-pass filtered (linear-phase Kaiser FIR, flat to 400 Hz,
at least 60 dB down from 500 Hz) and resampled to exactly 1 kHz. A 1 kHz source
is used as it is. A slower source is interpolated with the same band-limited
filter and flagged, because interpolation cannot add the missing fluctuations.

Gaps between bursts are found from the host timestamps. A window with gaps is
refused unless the caller explicitly asks for bursts to be joined end to end;
the provenance then says so, with the time that was left out.
"""
from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import tempfile

import numpy as np

from .. import __version__
from ..storage import ns_to_utc

TARGET_RATE_HZ = 1000
PASSBAND, STOPBAND, ATTENUATION_DB = 0.4, 0.5, 60.0   # Fractions of the lower of the two rates
GAP_TOLERANCE_S = 0.005    # Host-timestamp jitter allowance before a gap is declared
FLAG_LEVEL = 0.01          # An output sample is flagged if flagged inputs carry this weight in it
JOIN_WARNING = ("Bursts were joined end to end. The recording is not continuous: the gaps between bursts "
                "were left out, so the trace has a seam at every burst boundary and runs faster than real time.")


# --------------------------------------------------------------------------
# Gaps
# --------------------------------------------------------------------------

def burst_gaps(started_ns: np.ndarray, span_s: float) -> np.ndarray:
    """Unrecorded time after each burst but the last: start-to-start interval minus the stored span."""
    return np.diff(np.asarray(started_ns, dtype=np.int64)) / 1e9 - span_s


def gap_mask(gaps: np.ndarray, rate_hz: float) -> np.ndarray:
    return gaps > max(2.0 / rate_hz, GAP_TOLERANCE_S)


def longest_gap_free(started_ns: np.ndarray, span_s: float, rate_hz: float) -> tuple[int, int, float]:
    """(first burst, number of bursts, seconds) of the longest stretch with no gap."""
    n = len(started_ns)
    if n == 0:
        return 0, 0, 0.0
    breaks = np.flatnonzero(gap_mask(burst_gaps(started_ns, span_s), rate_hz))
    starts = np.concatenate([[0], breaks + 1])
    ends = np.concatenate([breaks, [n - 1]])
    seconds = (np.asarray(started_ns)[ends] - np.asarray(started_ns)[starts]) / 1e9 + span_s
    best = int(np.argmax(seconds))
    return int(starts[best]), int(ends[best] - starts[best] + 1), float(seconds[best])


def gap_summary(started_ns: np.ndarray, span_s: float, rate_hz: float) -> dict:
    gaps = burst_gaps(started_ns, span_s)
    real = gaps[gap_mask(gaps, rate_hz)]
    first, bursts, seconds = longest_gap_free(started_ns, span_s, rate_hz)
    return {"gaps": int(real.size),
            "gap_median_ms": float(np.median(real) * 1e3) if real.size else 0.0,
            "gap_max_ms": float(real.max() * 1e3) if real.size else 0.0,
            "missing_s": float(real.sum()),
            "interval_median_s": float(np.median(np.diff(started_ns)) / 1e9) if len(started_ns) > 1 else None,
            "longest_gap_free_s": seconds, "longest_gap_free_first_burst": first,
            "longest_gap_free_bursts": bursts}


# --------------------------------------------------------------------------
# Anti-alias filtering and resampling
# --------------------------------------------------------------------------

def kaiser_lowpass(f_pass: float, f_stop: float, attenuation_db: float = ATTENUATION_DB) -> np.ndarray:
    """Unity-DC-gain linear-phase Kaiser low-pass; edges in cycles per sample (same design as the recorder)."""
    beta = 0.1102 * (attenuation_db - 8.7)
    taps = math.ceil((attenuation_db - 8) / (2.285 * 2 * math.pi * (f_stop - f_pass))) + 1
    taps += 1 - taps % 2
    cutoff = (f_pass + f_stop) / 2
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.kaiser(taps, beta)
    return h / h.sum()


class Resampler:
    """Streaming rational resampler: zero-stuff by L, low-pass, keep every M-th sample.

    Only fully overlapped outputs are produced (no edge transients), so output 0
    lies ``delay_s`` after input 0. Feeding the input in any chunk sizes gives
    the same output as feeding it at once.
    """

    def __init__(self, rate_in: int, rate_out: int = TARGET_RATE_HZ):
        if int(rate_in) != rate_in or rate_in <= 0:
            raise ValueError(f"Source rate {rate_in} Hz is not a positive whole number")
        ratio = Fraction(int(rate_out), int(rate_in))
        self.rate_in, self.rate_out = int(rate_in), int(rate_out)
        self.L, self.M = ratio.numerator, ratio.denominator
        self.upsampled = rate_in < rate_out
        if self.L == self.M == 1:
            self.h = None
            self.delay_s = 0.0
            self.f_pass_hz = self.f_stop_hz = None
        else:
            fs = self.rate_in * self.L
            edge = min(self.rate_in, self.rate_out)
            self.f_pass_hz, self.f_stop_hz = PASSBAND * edge, STOPBAND * edge
            self.h = kaiser_lowpass(self.f_pass_hz / fs, self.f_stop_hz / fs) * self.L
            self._reversed = self.h[::-1].copy()
            self.delay_s = (len(self.h) - 1) / 2 / fs
        self._buffer = np.empty(0)
        self._offset = 0

    @property
    def taps(self) -> int:
        return 1 if self.h is None else len(self.h)

    def describe(self) -> dict:
        if self.h is None:
            return {"rate_in_hz": self.rate_in, "rate_out_hz": self.rate_out, "filter": "none (already 1 kHz)"}
        return {"rate_in_hz": self.rate_in, "rate_out_hz": self.rate_out, "up": self.L, "down": self.M,
                "filter": "linear-phase Kaiser FIR, unity DC gain", "taps": self.taps,
                "passband_hz": self.f_pass_hz, "stopband_hz": self.f_stop_hz, "attenuation_db": ATTENUATION_DB,
                "delay_s": self.delay_s, "interpolated_from_slower_source": self.upsampled}

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        if self.h is None:
            return x.copy()
        up = np.zeros(x.size * self.L)
        up[::self.L] = x
        data = np.concatenate([self._buffer, up])
        rows = data.size - len(self.h) + 1
        if rows <= self._offset:
            self._buffer = data
            return np.empty(0)
        windows = np.lib.stride_tricks.sliding_window_view(data, len(self.h))[self._offset::self.M]
        y = windows @ self._reversed
        following = self._offset + len(y) * self.M
        self._buffer = data[following:]
        self._offset = 0
        return y


# --------------------------------------------------------------------------
# Window selection and preparation
# --------------------------------------------------------------------------

def parse_start(session, start) -> int:
    """Start time in host ns: seconds after the first burst, or an ISO 8601 UTC time."""
    if start is None:
        return int(session.started_ns[0])
    text = str(start)
    try:
        return int(session.started_ns[0]) + round(float(text) * 1e9)
    except ValueError:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return round(moment.timestamp() * 1e6) * 1000


def input_flags(session, volts, rails):
    """Per-sample flags: below the SUM threshold, saturated or at the ADC rail."""
    low, high = session.thresholds()
    weak = volts < low
    saturated = volts >= high
    if rails is not None:
        saturated |= rails
    return weak, saturated


def prepare(session, out: Path, start=None, duration_s: float | None = None, dark_v: float | None = None,
            join_bursts: bool = False, progress=None) -> dict:
    """Write ``out`` (CSV: t_s, h, sum_v, flagged) and ``out.json`` (provenance). Returns the provenance."""
    out = Path(out)
    start_ns = parse_start(session, start)
    first = int(np.searchsorted(session.started_ns, start_ns))
    if first >= session.bursts:
        raise ValueError(f"No bursts start at or after {ns_to_utc(start_ns)} in {session.id}")
    resampler = Resampler(session.rate_hz)
    wanted = None if duration_s is None else round(duration_s * TARGET_RATE_HZ)
    if wanted is not None and wanted <= 0:
        raise ValueError("--duration must be positive")

    # Bursts the window will use (an upper bound when the duration is given).
    if wanted is None:
        stop = session.bursts
    else:
        need_s = wanted / TARGET_RATE_HZ + 2 * resampler.delay_s
        stop = min(session.bursts, first + math.ceil(need_s / session.span_s) + 1)
    window_gaps = gap_summary(session.started_ns[first:stop], session.span_s, session.rate_hz)
    if window_gaps["gaps"] and not join_bursts:
        whole = gap_summary(session.started_ns, session.span_s, session.rate_hz)
        best = whole["longest_gap_free_first_burst"]
        raise ValueError(
            f"The window has {window_gaps['gaps']} gaps between bursts (median {window_gaps['gap_median_ms']:.0f} ms, "
            f"longest {window_gaps['gap_max_ms']:.0f} ms): this recording is burst-mode, not continuous. "
            f"The longest gap-free stretch in {session.id} is {whole['longest_gap_free_s']:.3f} s, starting "
            f"{ns_to_utc(int(session.started_ns[best]))}. Use --join-bursts to join bursts end to end anyway; "
            "the provenance will record that the trace is not continuous.")

    flag_resampler = Resampler(session.rate_hz)
    counts = {"input": 0, "weak": 0, "saturated": 0, "clipped_any_channel": 0}
    produced, last_burst = 0, first
    flagged_out = 0
    total = total_sq = 0.0
    dark = 0.0 if dark_v is None else float(dark_v)
    with tempfile.TemporaryFile() as scratch:
        for index, volts, rails, clipped in session.chunks(first, stop):
            if wanted is not None and produced >= wanted:
                break
            weak, saturated = input_flags(session, volts, rails)
            counts["input"] += volts.size
            counts["weak"] += int(weak.sum())
            counts["saturated"] += int(saturated.sum())
            if clipped is not None:
                counts["clipped_any_channel"] += int(clipped.sum())
            y = resampler.process(volts.ravel())
            f = flag_resampler.process((weak | saturated).ravel().astype(float))
            if wanted is not None:
                y, f = y[:wanted - produced], f[:wanted - produced]
            flag = np.abs(f) > FLAG_LEVEL
            np.column_stack([y, flag]).astype(np.float64).tofile(scratch)
            produced += y.size
            flagged_out += int(flag.sum())
            total += float((y - dark).sum())
            total_sq += float(((y - dark) ** 2).sum())
            last_burst = index + volts.shape[0] - 1
            if progress:
                progress(produced, wanted)
        if produced == 0:
            raise ValueError("The window is shorter than the anti-alias filter; choose a longer window")
        if wanted is not None and produced < wanted:
            print(f"Warning: the session ends {produced / TARGET_RATE_HZ:.1f} s into the window; "
                  f"the trace is shorter than the {wanted / TARGET_RATE_HZ:.1f} s asked for.")
        mean = total / produced
        if mean <= 0:
            raise ValueError(f"The mean SUM over the window ({mean + dark:.4f} V) is not above the dark level ({dark} V)")
        variance = max(total_sq / produced - mean ** 2, 0.0)

        # Second pass: normalise and write the trace.
        scratch.seek(0)
        digest = hashlib.sha256()
        minimum, maximum, nonpositive = math.inf, -math.inf, 0
        with open(out, "w", encoding="ascii", newline="\n") as file:
            header = "t_s,h,sum_v,flagged\n"
            file.write(header)
            digest.update(header.encode())
            written = 0
            while True:
                block = np.fromfile(scratch, dtype=np.float64, count=2 * 65536)
                if not block.size:
                    break
                y, flag = block[0::2], block[1::2].astype(int)
                h = (y - dark) / mean
                minimum, maximum = min(minimum, h.min()), max(maximum, h.max())
                nonpositive += int((h <= 0).sum())
                t = (written + np.arange(y.size)) / TARGET_RATE_HZ
                text = "".join(f"{a:.3f},{b:.6f},{c:.5f},{d}\n" for a, b, c, d in zip(t, h, y, flag))
                file.write(text)
                digest.update(text.encode())
                written += y.size

    used = slice(first, last_burst + 1)
    used_gaps = gap_summary(session.started_ns[used], session.span_s, session.rate_hz)
    wall_s = (int(session.started_ns[last_burst]) - int(session.started_ns[first])) / 1e9 + session.span_s
    low, high = session.thresholds()
    provenance = {
        "tool": f"oclab.voa {__version__}",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trace_file": out.name, "trace_sha256": digest.hexdigest(),
        "columns": {"t_s": "playback time (s) at 1 kHz", "h": "normalised received power (SUM - dark) / mean",
                    "sum_v": "filtered and resampled SUM (V), before dark subtraction",
                    "flagged": "1 if a SUM sample under the filter was saturated, at the ADC rail or below the SUM threshold"},
        "source": {"path": str(session.path), "kind": session.kind, "session_id": session.id,
                   "session_started_utc": session.started_utc, "session_ended_utc": session.ended_utc,
                   "schema_version": session.metadata.get("schema_version"),
                   "driver_revision": session.metadata.get("driver_revision"),
                   "scope": session.metadata.get("settings", {}).get("scope"),
                   "calibration": session.calibration, "experiment": session.metadata.get("settings", {}).get("experiment")},
        "window": {"requested_start_utc": ns_to_utc(start_ns), "requested_duration_s": duration_s,
                   "first_capture_id": int(session.capture_id[first]),
                   "first_burst_started_utc": ns_to_utc(int(session.started_ns[first])),
                   "last_capture_id": int(session.capture_id[last_burst]),
                   "last_burst_received_utc": ns_to_utc(int(session.received_ns[last_burst])),
                   "bursts": last_burst - first + 1, "wall_clock_span_s": wall_s,
                   "samples": produced, "duration_s": produced / TARGET_RATE_HZ},
        "continuity": {"continuous": used_gaps["gaps"] == 0, "bursts_joined": used_gaps["gaps"] > 0,
                       "warning": JOIN_WARNING if used_gaps["gaps"] else None,
                       "burst_span_s": session.span_s, **used_gaps},
        "resampling": resampler.describe(),
        "dark": {"value_v": dark, "source": "given with --dark" if dark_v is not None else
                 "assumed 0 V: no beam-blocked measurement was given"},
        "normalisation": {"mean_sum_minus_dark_v": mean, "mean_sum_v": mean + dark},
        "flags": {"sum_threshold_v": low, "saturation_or_rail_v": high, "rail_v": session.rail_v,
                  "input_samples": counts["input"],
                  "input_below_threshold_fraction": counts["weak"] / counts["input"],
                  "input_saturated_fraction": counts["saturated"] / counts["input"],
                  "input_clipped_any_channel_fraction": counts["clipped_any_channel"] / counts["input"],
                  "output_flagged_fraction": flagged_out / produced,
                  "note": "Flagged samples are kept and marked, not removed: removing them would add gaps. "
                          "Saturation cuts the peaks of the fading, so a high flagged fraction biases h."},
        "statistics": {"scintillation_index": variance / mean ** 2, "h_min": float(minimum), "h_max": float(maximum),
                       "h_nonpositive_fraction": nonpositive / produced},
    }
    out.with_suffix(".json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    return provenance


# --------------------------------------------------------------------------
# Survey
# --------------------------------------------------------------------------

def true_runs(mask: np.ndarray):
    """(start, length) of each run of True, longest first."""
    padded = np.concatenate([[False], mask, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    runs = [(int(a), int(b - a)) for a, b in zip(edges[::2], edges[1::2])]
    return sorted(runs, key=lambda r: -r[1])


def survey(session, max_flagged: float = 0.01, dark_below_v: float = 0.05, min_dark_bursts: int = 20) -> dict:
    """Timing, gaps, flags, windows suitable for playback and dark candidates for one session."""
    flagged, means, stds = [], [], []
    clipped_any = total = 0
    for _, volts, rails, clipped in session.chunks():
        weak, saturated = input_flags(session, volts, rails)
        flagged.append((weak | saturated).mean(axis=1))
        means.append(volts.mean(axis=1))
        stds.append(volts.std(axis=1))
        total += volts.size
        if clipped is not None:
            clipped_any += int(clipped.sum())
    flagged, means, stds = np.concatenate(flagged), np.concatenate(means), np.concatenate(stds)
    span = session.span_s
    result = {
        "session_id": session.id, "started_utc": session.started_utc, "ended_utc": session.ended_utc,
        "bursts": session.bursts, "rate_hz": session.rate_hz, "samples_per_burst": session.samples_per_burst,
        "burst_span_s": span,
        "wall_clock_s": (int(session.started_ns[-1]) - int(session.started_ns[0])) / 1e9 + span,
        "recorded_s": session.bursts * span,
        **gap_summary(session.started_ns, span, session.rate_hz),
        "sum_mean_v": float(means.mean()),
        "flagged_fraction": float(flagged.mean()),
        "clipped_any_channel_fraction": clipped_any / total if total else 0.0,
        "windows": [], "dark_candidates": [],
    }
    result["coverage"] = result["recorded_s"] / result["wall_clock_s"]
    for first, length in true_runs(flagged <= max_flagged)[:3]:
        result["windows"].append({
            "start_utc": ns_to_utc(int(session.started_ns[first])), "start_s": (int(session.started_ns[first]) - int(session.started_ns[0])) / 1e9,
            "bursts": length, "joined_s": length * span,
            "wall_clock_s": (int(session.started_ns[first + length - 1]) - int(session.started_ns[first])) / 1e9 + span,
            "flagged_fraction": float(flagged[first:first + length].mean())})
    for first, length in true_runs(np.abs(means) < dark_below_v)[:2]:
        if length >= min_dark_bursts:
            part = slice(first, first + length)
            result["dark_candidates"].append({
                "start_utc": ns_to_utc(int(session.started_ns[first])), "bursts": length,
                "mean_v": float(means[part].mean()), "within_burst_std_v": float(np.median(stds[part]))})
    return result
