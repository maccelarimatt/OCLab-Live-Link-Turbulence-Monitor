"""Anti-alias filtering and decimation of scope bursts to the stored rate.

The scope cannot sample below 25 kS/s. Each burst is low-pass filtered with
a linear-phase Kaiser FIR and every `factor`-th output kept, so the archive
holds a band-limited series at the configured store rate. Only "valid"
convolution outputs are kept: no edge transients, at the cost of
(taps - 1) input samples per burst.
"""
from __future__ import annotations

from functools import lru_cache
import math

import numpy as np

PASSBAND = 0.4         # usable bandwidth as a fraction of the store rate
STOPBAND = 0.5         # full attenuation by the output Nyquist frequency
ATTENUATION_DB = 60.0  # beyond the scope's 8-bit (48 dB) dynamic range
CODE_SCALE = 64        # stored int16 = ADC codes x 64 about mid-scale
CODES_PER_DIV = 31     # driver: volts = (code - 128) / 31 * volts_per_div
MIN_STORED = 64        # reject settings leaving fewer samples per burst


@lru_cache(maxsize=16)
def fir(factor: int) -> np.ndarray | None:
    """Unity-DC-gain Kaiser low-pass for decimation by `factor` (None for 1)."""
    if factor == 1:
        return None
    f_pass, f_stop = PASSBAND / factor, STOPBAND / factor  # cycles per input sample
    beta = 0.1102 * (ATTENUATION_DB - 8.7)
    taps = math.ceil((ATTENUATION_DB - 8) / (2.285 * 2 * math.pi * (f_stop - f_pass))) + 1
    taps += 1 - taps % 2  # odd length: integer group delay
    cutoff = (f_pass + f_stop) / 2
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.kaiser(taps, beta)
    return h / h.sum()


def plan(sample_rate: int, store_rate: int, depth: int) -> dict:
    """Decimation parameters; raises ValueError for unusable combinations."""
    if type(store_rate) is not int or store_rate <= 0:
        raise ValueError("store_rate_hz must be a positive integer")
    if store_rate > sample_rate or sample_rate % store_rate:
        choices = [sample_rate // f for f in range(1, 10_001)
                   if sample_rate % f == 0 and sample_rate // f >= 100][:12]
        raise ValueError(f"store_rate_hz must divide the scope sample_rate ({sample_rate}); "
                         f"for example {', '.join(map(str, choices))}")
    factor = sample_rate // store_rate
    h = fir(factor)
    taps = 1 if h is None else len(h)
    stored = (depth - taps) // factor + 1
    if stored < MIN_STORED:
        raise ValueError(f"store_rate_hz {store_rate} leaves only {max(stored, 0)} samples per {depth}-sample burst; "
                         "raise store_rate_hz, depth or lower the scope sample_rate")
    return {"factor": factor, "taps": taps, "stored": stored,
            "bandwidth_hz": store_rate * (0.5 if factor == 1 else PASSBAND),
            "t0_s": (taps - 1) / 2 / sample_rate}


def decimate(values: np.ndarray, factor: int) -> np.ndarray:
    """Filter and decimate the last axis of a float array."""
    h = fir(factor)
    if h is None:
        return values.astype(float)
    windows = np.lib.stride_tricks.sliding_window_view(values, len(h), axis=-1)[..., ::factor, :]
    return windows @ h[::-1]


def any_in_window(flags: np.ndarray, factor: int) -> np.ndarray:
    """True for an output sample if any input sample under its filter was flagged."""
    h = fir(factor)
    if h is None:
        return flags.copy()
    return np.lib.stride_tricks.sliding_window_view(flags, len(h))[::factor].any(axis=1)


def to_codes(volts: np.ndarray, volts_per_div) -> np.ndarray:
    """Scope volts (before probe factors) -> int16 ADC codes x CODE_SCALE about mid-scale."""
    scale = CODES_PER_DIV * CODE_SCALE / np.asarray(volts_per_div, dtype=float)[:, None]
    return np.clip(np.rint(volts * scale), -32768, 32767).astype(np.int16)


def volts_per_count(volts_per_div, probe_factors) -> np.ndarray:
    """Multiply stored int16 counts by this (per channel) for probe-corrected volts."""
    return np.asarray(volts_per_div, dtype=float) * np.asarray(probe_factors, dtype=float) / (CODES_PER_DIV * CODE_SCALE)
