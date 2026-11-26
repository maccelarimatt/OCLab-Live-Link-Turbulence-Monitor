"""Compare the played target with the photodetector monitor logged during playback.

Both series are normalised to their own means before comparison, because the
INTENSITY output is a scaled copy that may not hold the mean level (the board
has a DC-subtraction loop). The monitor is aligned to the target by the lag
that maximises their correlation, searched over a few milliseconds.
"""
from __future__ import annotations

import numpy as np

QUANTILES = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)
FADE_THRESHOLDS_DB = (-3.0, -6.0, -10.0)
BANDS_HZ = ((0.1, 1.0), (1.0, 10.0), (10.0, 100.0), (100.0, 500.0))


def normalise(x) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    mean = x.mean()
    if mean <= 0:
        raise ValueError("The mean is not positive; check the dark level")
    return x / mean


def scintillation_index(x) -> float:
    x = np.asarray(x, dtype=float)
    return float(x.var() / x.mean() ** 2)


def welch_psd(x, fs: float, segment: int = 4096):
    """One-sided power spectral density of x - mean (Hann window, 50 % overlap)."""
    x = np.asarray(x, dtype=float) - np.mean(x)
    segment = min(segment, x.size)
    if segment < 16:
        raise ValueError("Too few samples for a spectrum")
    window = np.hanning(segment)
    step = segment // 2
    starts = range(0, x.size - segment + 1, step)
    spectra = [np.abs(np.fft.rfft((x[s:s + segment] - x[s:s + segment].mean()) * window)) ** 2 for s in starts]
    psd = np.mean(spectra, axis=0) / (fs * (window ** 2).sum())
    psd[1:-1] *= 2
    return np.fft.rfftfreq(segment, 1 / fs), psd


def band_powers(freq, psd, bands=BANDS_HZ) -> list[dict]:
    df = freq[1] - freq[0]
    return [{"band_hz": [lo, hi], "variance": float(psd[(freq >= lo) & (freq < hi)].sum() * df)} for lo, hi in bands]


def fade_statistics(h, fs: float, thresholds_db=FADE_THRESHOLDS_DB) -> list[dict]:
    """Probability of being below each threshold (dB about the mean), fades per second, mean fade length."""
    h = np.asarray(h, dtype=float)
    out = []
    for level_db in thresholds_db:
        below = h < 10 ** (level_db / 10)
        edges = np.diff(np.concatenate([[0], below.astype(np.int8), [0]]))
        starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
        lengths = (ends - starts) / fs
        out.append({"threshold_db": level_db, "probability": float(below.mean()),
                    "fades_per_s": len(starts) / (h.size / fs),
                    "mean_duration_ms": float(lengths.mean() * 1e3) if len(lengths) else 0.0,
                    "max_duration_ms": float(lengths.max() * 1e3) if len(lengths) else 0.0})
    return out


def best_lag(target, monitor, max_lag: int) -> int:
    """Lag k (samples) maximising corr(target[i], monitor[i + k]) for |k| <= max_lag."""
    a, b = np.asarray(target, float), np.asarray(monitor, float)
    a, b = a - a.mean(), b - b.mean()
    best, best_r = 0, -np.inf
    for k in range(-max_lag, max_lag + 1):
        x, y = (a[:a.size - k], b[k:]) if k >= 0 else (a[-k:], b[:b.size + k])
        denominator = np.sqrt((x * x).sum() * (y * y).sum())
        r = (x * y).sum() / denominator if denominator > 0 else -np.inf
        if r > best_r:
            best, best_r = k, r
    return best


def shift(target, monitor, lag: int):
    if lag >= 0:
        return target[:target.size - lag], monitor[lag:]
    return target[-lag:], monitor[:monitor.size + lag]


def compare(target, monitor, fs: float = 1000.0, max_lag: int = 5) -> dict:
    """Statistics of the normalised target and monitor, and the error between them in dB."""
    target, monitor = np.asarray(target, float), np.asarray(monitor, float)
    lag = best_lag(target, monitor, max_lag)
    target, monitor = shift(target, monitor, lag)
    ht, hm = normalise(target), normalise(monitor)
    freq, pt = welch_psd(ht, fs)
    _, pm = welch_psd(hm, fs)
    positive = (ht > 0) & (hm > 0)
    error_db = 10 * np.log10(hm[positive] / ht[positive])
    edges = np.linspace(0, max(ht.max(), hm.max()) * 1.0001, 61)
    pdf_t, _ = np.histogram(ht, edges, density=True)
    pdf_m, _ = np.histogram(hm, edges, density=True)
    return {
        "samples": int(ht.size), "lag_samples": lag,
        "correlation": float(np.corrcoef(ht, hm)[0, 1]),
        "scintillation_index": {"target": scintillation_index(ht), "monitor": scintillation_index(hm)},
        "quantiles": {"q": list(QUANTILES), "target": np.quantile(ht, QUANTILES).tolist(),
                      "monitor": np.quantile(hm, QUANTILES).tolist()},
        "pdf_max_difference": float(np.abs(pdf_t - pdf_m).max() * (edges[1] - edges[0])),
        "ks_distance": float(np.abs(np.searchsorted(np.sort(ht), edges, "right") / ht.size
                                    - np.searchsorted(np.sort(hm), edges, "right") / hm.size).max()),
        "bands": [{"band_hz": t["band_hz"], "target": t["variance"], "monitor": m["variance"],
                   "ratio_db": float(10 * np.log10(m["variance"] / t["variance"])) if t["variance"] > 0 and m["variance"] > 0 else None}
                  for t, m in zip(band_powers(freq, pt), band_powers(freq, pm))],
        "fades": {"target": fade_statistics(ht, fs), "monitor": fade_statistics(hm, fs)},
        "error_db": {"mean": float(error_db.mean()), "rms": float(np.sqrt((error_db ** 2).mean())),
                     "p95_abs": float(np.quantile(np.abs(error_db), 0.95)),
                     "excluded_nonpositive": int((~positive).sum())},
        "spectra": {"freq_hz": freq.tolist(), "target": pt.tolist(), "monitor": pm.tolist()},
        "pdf": {"edges": edges.tolist(), "target": pdf_t.tolist(), "monitor": pdf_m.tolist()},
    }
