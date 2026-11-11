from __future__ import annotations

import logging
import time
import numpy as np
from hantek6254bc import Capture, Hantek6254BC

from .config import Settings
from .decimation import any_in_window, decimate, plan, to_codes

logger = logging.getLogger(__name__)
CALIBRATION_ATTEMPTS = 5


class CalibrationUnavailable(RuntimeError):
    pass


def calibrated(scope, volts_per_div) -> bool:
    """True when the unit's zero-level table loaded and gives a value for every channel's range."""
    if getattr(scope, "_position_calibration", None) is None:
        return False
    per_channel = getattr(scope, "_position_pwm", None)  # None per channel means the driver used its fallback
    return per_channel is None or all(per_channel(ch, vdiv) is not None for ch, vdiv in enumerate(volts_per_div))


class HardwareSource:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.scope = None

    def open(self):
        """Open the scope, insisting that its own zero-level calibration loaded.

        The driver reads per-unit zero-level (position) and gain tables from the
        scope's EEPROM over USB. If that read fails it silently uses generic
        values, which on this unit shifts every channel by about +40 ADC codes
        (+1.2 V at 1 V/div) and puts a centred beam in the top-right corner.
        Reopen until the table loads; never record with the fallback.
        """
        s = self.settings.scope
        for attempt in range(1, CALIBRATION_ATTEMPTS + 1):
            scope = Hantek6254BC.open(s.driver_config(), bus=s.bus, address=s.address)
            if calibrated(scope, s.volts_per_div):
                if getattr(scope, "_scale_calibration", None) is None:
                    logger.warning("Scope gain calibration table did not load; voltages use nominal gain")
                if attempt > 1:
                    logger.info("Scope zero-level calibration loaded on attempt %d", attempt)
                return scope
            logger.warning("Scope zero-level calibration did not load (attempt %d of %d); reopening",
                           attempt, CALIBRATION_ATTEMPTS)
            scope.close()
            time.sleep(0.5)
        raise CalibrationUnavailable("The oscilloscope's zero-level calibration could not be read, "
                                     "so readings would carry a false offset")

    def capture(self) -> Capture:
        if self.scope is None:
            self.scope = self.open()
        return self.scope.capture()

    def close(self):
        if self.scope is not None:
            try:
                self.scope.close()
            finally:
                self.scope = None


class DemoSource:
    """Synthetic waveforms; never selected automatically on hardware errors."""

    TRANSFER_S = 0.03  # Simulated USB transfer and re-arm gap between bursts

    def __init__(self, settings: Settings):
        self.settings = settings
        self.rng = np.random.default_rng()
        self.started = time.monotonic()

    def capture(self) -> Capture:
        s = self.settings.scope
        # Take as long as a real scope: record the burst, then transfer it.
        time.sleep(s.depth / s.sample_rate + self.TRANSFER_S)
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


def positions(x, y, total, settings: Settings, valid):
    """Per-sample position from probe-corrected X/Y/SUM volts under the configured model."""
    c = settings.calibration
    n = x.shape[-1]
    corrected_sum = total - c.sum_offset_v
    px = x - c.x_offset_v
    py = y - c.y_offset_v
    if c.position_model == "difference_over_sum":
        px = np.divide(px, corrected_sum, out=np.zeros(n), where=valid)
        py = np.divide(py, corrected_sum, out=np.zeros(n), where=valid)
    unit = "ratio" if c.position_model == "difference_over_sum" else "V"
    if c.x_scale_mm is not None:
        px, py = px * c.x_scale_mm, py * c.y_scale_mm
        unit = "mm"
    return px, py, unit


def saturated(x, y, total, calibration):
    """Samples where the detector itself is at its output limit (not the scope's ADC rails)."""
    limit = calibration.saturation_v
    if limit is None:
        return np.zeros(x.shape, dtype=bool)
    return (total > limit) | (np.abs(x) > limit) | (np.abs(y) > limit)


def decimate_capture(capture: Capture, settings: Settings) -> tuple[dict, dict]:
    """Anti-alias filter and decimate a burst to store_rate_hz.

    Returns the archived arrays and summary fields computed on the stored
    (band-limited) series, including the scintillation index of SUM.
    """
    s, c = settings.scope, settings.calibration
    p = plan(s.sample_rate, s.store_rate_hz, s.depth)
    volts = decimate(capture.volts, p["factor"])
    indices = np.array([s.x_channel, s.y_channel, s.sum_channel]) - 1
    rails = np.any((capture.raw[indices] <= 28) | (capture.raw[indices] >= 227), axis=0)
    clipped = any_in_window(rails, p["factor"])
    full = capture.volts[indices] * np.asarray(s.probe_factors)[indices, None]
    detector_limit = any_in_window(saturated(*full, c), p["factor"])
    total = volts[indices[2]] * s.probe_factors[indices[2]] - c.sum_offset_v
    valid = (total >= c.minimum_sum_v) & ~clipped & ~detector_limit
    scint = None
    if valid.sum() >= 2 and total[valid].mean() > 0:
        scint = float(np.var(total[valid]) / total[valid].mean() ** 2)
    stored = {"codes": to_codes(volts, s.volts_per_div), "clipped": clipped,
              "store_rate_hz": s.store_rate_hz, "t0_s": p["t0_s"]}
    return stored, {"store_rate_hz": s.store_rate_hz, "stored_samples": p["stored"],
                    "bandwidth_hz": p["bandwidth_hz"], "scintillation_index": scint,
                    "stored_valid_fraction": float(valid.mean())}


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
    unused = ({0, 1, 2, 3} - set(indices.tolist())).pop()
    x, y, total = v[indices]
    corrected_sum = total - c.sum_offset_v
    # These are the upstream driver's wire-code rails (nominal codes 0/255).
    clipped = np.any((capture.raw[indices] <= 28) | (capture.raw[indices] >= 227), axis=0)
    over = saturated(x, y, total, c)
    valid = (corrected_sum >= c.minimum_sum_v) & ~clipped & ~over
    px, py, unit = positions(x, y, total, settings, valid)

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
        "saturated_fraction": float(over.mean()),
        # The channel not mapped to X/Y/SUM; when left unconnected it should read 0 V, so a
        # non-zero mean reveals a scope zero-level offset shared by every channel.
        "unused_channel": unused + 1, "unused_channel_mean_v": float(v[unused].mean()),
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
