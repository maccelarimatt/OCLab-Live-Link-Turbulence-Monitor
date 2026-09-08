from __future__ import annotations

import time
import numpy as np
from hantek6254bc import Capture, Hantek6254BC

from .config import Settings


class HardwareSource:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.scope = None

    def capture(self) -> Capture:
        if self.scope is None:
            s = self.settings.scope
            self.scope = Hantek6254BC.open(s.driver_config(), bus=s.bus, address=s.address)
        return self.scope.capture()

    def close(self):
        if self.scope is not None:
            try:
                self.scope.close()
            finally:
                self.scope = None


class DemoSource:
    """Synthetic waveforms; never selected automatically on hardware errors."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.rng = np.random.default_rng()
        self.started = time.monotonic()

    def capture(self) -> Capture:
        s = self.settings.scope
        t = np.arange(s.depth, dtype=float) / s.sample_rate
        phase = time.monotonic() - self.started + t
        total = 1.7 + 0.16 * np.sin(phase * 0.63) + self.rng.normal(0, 0.018, s.depth)
        x = total * (0.09 * np.sin(phase * 0.7) + 0.025 * np.sin(phase * 2.6))
        y = total * (0.065 * np.cos(phase * 0.93) + 0.02 * np.sin(phase * 3.2))
        volts = self.rng.normal(0, 0.008, (4, s.depth))
        for channel, values in ((s.x_channel, x), (s.y_channel, y), (s.sum_channel, total)):
            volts[channel - 1] += values
        scale = np.array(s.volts_per_div)[:, None]
        raw = np.clip(np.rint((volts / scale * 31 + 128) * 200 / 256 + 28), 28, 228).astype(np.uint8)
        # Use the same quantisation as the real driver's voltage conversion.
        volts = (np.clip(np.floor((raw.astype(float) - 28) * 256 / 200 + 0.5), 0, 255) - 128) / 31 * scale
        return Capture(time_s=t, volts=volts, raw=raw, sample_rate=s.sample_rate)

    def close(self):
        pass


def analyse(capture: Capture, settings: Settings) -> tuple[dict, dict]:
    s, c = settings.scope, settings.calibration
    n = s.depth
    if (capture.volts.shape != (4, n) or capture.raw.shape != (4, n)
            or capture.time_s.shape != (n,) or capture.sample_rate != s.sample_rate
            or not np.isfinite(capture.volts).all() or not np.isfinite(capture.time_s).all()
            or not np.allclose(capture.time_s, np.arange(n) / s.sample_rate, rtol=1e-8, atol=1e-12)):
        raise ValueError("Driver returned an invalid waveform")
    v = capture.volts * np.asarray(s.probe_factors)[:, None]
    indices = np.array([s.x_channel, s.y_channel, s.sum_channel]) - 1
    x, y, total = v[indices]
    corrected_sum = total - c.sum_offset_v
    # These are the upstream driver's wire-code rails (nominal codes 0/255).
    clipped = np.any((capture.raw[indices] <= 28) | (capture.raw[indices] >= 227), axis=0)
    valid = (corrected_sum >= c.minimum_sum_v) & ~clipped
    px = x - c.x_offset_v
    py = y - c.y_offset_v
    if c.position_model == "difference_over_sum":
        px = np.divide(px, corrected_sum, out=np.zeros(n), where=valid)
        py = np.divide(py, corrected_sum, out=np.zeros(n), where=valid)
    unit = "ratio" if c.position_model == "difference_over_sum" else "V"
    if c.x_scale_mm is not None:
        px, py = px * c.x_scale_mm, py * c.y_scale_mm
        unit = "mm"

    def mean(a):
        return float(np.mean(a[valid])) if valid.any() else None

    summary = {
        "sample_count": n, "sample_rate_hz": capture.sample_rate,
        "burst_duration_s": n / capture.sample_rate,
        "x_mean_v": float(x.mean()), "y_mean_v": float(y.mean()), "sum_mean_v": float(total.mean()),
        "x_std_v": float(x.std()), "y_std_v": float(y.std()), "sum_std_v": float(total.std()),
        "position_x": mean(px), "position_y": mean(py), "position_unit": unit,
        "wander_rms": float(np.sqrt(np.var(px[valid]) + np.var(py[valid]))) if valid.any() else None,
        "valid_fraction": float(valid.mean()), "clipped_fraction": float(clipped.mean()),
        "low_sum_fraction": float((corrected_sum < c.minimum_sum_v).mean()),
    }
    # Only the browser preview is reduced. The archive retains every sample.
    step = max(1, int(np.ceil(n / 256)))
    pick = np.arange(0, n, step)
    preview = {
        "time_s": capture.time_s[pick].tolist(),
        "x_v": x[pick].tolist(), "y_v": y[pick].tolist(), "sum_v": total[pick].tolist(),
        "position_x": [float(px[i]) if valid[i] else None for i in pick],
        "position_y": [float(py[i]) if valid[i] else None for i in pick],
    }
    return summary, preview
