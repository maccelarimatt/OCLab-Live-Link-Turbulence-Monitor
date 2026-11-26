"""VOA calibration and conversion of a fading trace into drive codes.

The VOA is a bright type: 0 V transmits the most light and more voltage
attenuates more. The recorded fading therefore never goes to the VOA as a
voltage; each target power is converted through the measured calibration,
interpolating log power against voltage on a curve forced to be monotonic.

Codes are 12-bit (0 to 4095). With the default PWM output the Arduino plays
level n = (code + 2) >> 2 of 1024, giving n / 1024 of its 5V-pin voltage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path

import numpy as np

MAX_DRIVE_V = 5.0           # Software limit; the buffer can reach about 5.8 V and the VOA is rated to 6.5 V
CODE_FULL_SCALE = 4096
MAX_CODE = 4095
PWM_LEVELS = 1024
ADC_FULL_SCALE = 1024
ADC_SATURATED = 1015        # A0 codes at or above this are treated as clipped
USABLE_TRANSMISSION = 0.95  # Top of the playable range, as a fraction of the 0 V transmission
MIN_CALIBRATION_POINTS = 5
VDD_RANGE = (4.5, 5.5)      # Plausible measured 5V-pin voltages


def check_supply(vdd: float, vmax: float):
    if not (VDD_RANGE[0] <= vdd <= VDD_RANGE[1]):
        raise ValueError(f"--vdd {vdd} V is not a plausible 5V-pin voltage; measure it with a meter")
    if not (0 < vmax <= MAX_DRIVE_V):
        raise ValueError(f"--vmax must be above 0 and at most {MAX_DRIVE_V} V")


def code_limit(vdd: float, vmax: float) -> int:
    """Highest code whose output stays at or below vmax, for both the PWM and the 12-bit DAC output."""
    check_supply(vdd, vmax)
    level = math.floor(vmax / vdd * PWM_LEVELS + 1e-9)          # PWM level n gives n/1024 x vdd
    pwm = min(4 * level + 1, MAX_CODE)                          # (code + 2) >> 2 <= level
    dac = min(math.floor(vmax / vdd * CODE_FULL_SCALE + 1e-9), MAX_CODE)
    return max(0, min(pwm, dac))


def pwm_level(codes) -> np.ndarray:
    return (np.asarray(codes, dtype=np.int64) + 2) >> 2


def codes_to_volts(codes, vdd: float) -> np.ndarray:
    """Output voltage the PWM circuit produces for each code."""
    return pwm_level(codes) * vdd / PWM_LEVELS


def volts_to_codes(volts, vdd: float, vmax: float) -> np.ndarray:
    """Drive volts to codes, never above the code limit. Non-finite input is an error."""
    volts = np.asarray(volts, dtype=float)
    if not np.all(np.isfinite(volts)):
        raise ValueError("The drive contains non-finite values")
    limit = code_limit(vdd, vmax)
    codes = np.rint(np.clip(volts, 0.0, vmax) / vdd * CODE_FULL_SCALE)
    return np.clip(codes, 0, limit).astype(np.uint16)


def adc_to_volts(codes, vdd: float) -> np.ndarray:
    return np.asarray(codes, dtype=float) * vdd / ADC_FULL_SCALE


def uw_to_dbm(power_uw):
    return 10 * np.log10(np.asarray(power_uw, dtype=float) / 1000.0)


def dbm_to_uw(dbm: float) -> float:
    return 1000.0 * 10 ** (dbm / 10)


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------

@dataclass
class Calibration:
    voltage: np.ndarray        # Increasing, from the lowest calibrated voltage
    power: np.ndarray          # Strictly decreasing, positive, in `unit`
    unit: str                  # "uW" or "monitor_v"
    meta: dict = field(default_factory=dict)
    dropped: int = 0           # Points removed because they could not be inverted

    @property
    def power_max(self) -> float:
        """Top of the playable range: a margin below the 0 V transmission."""
        return USABLE_TRANSMISSION * float(self.power[0])

    @property
    def power_min(self) -> float:
        return float(self.power[-1])

    @property
    def range_db(self) -> float:
        return 10 * math.log10(self.power[0] / self.power[-1])

    def voltage_for(self, power) -> np.ndarray:
        """Invert the falling curve by interpolating log power against voltage."""
        target = np.clip(np.asarray(power, dtype=float), self.power_min, float(self.power[0]))
        # np.interp needs increasing x: log power falls with voltage, so reverse both.
        return np.interp(np.log10(target), np.log10(self.power[::-1]), self.voltage[::-1])


def monotonic_curve(voltage, power):
    """Sort by voltage, keep the leading positive points, and force power to fall strictly."""
    voltage, power = np.asarray(voltage, dtype=float), np.asarray(power, dtype=float)
    if voltage.size == 0 or not (np.all(np.isfinite(voltage)) and np.all(np.isfinite(power))):
        raise ValueError("The calibration is empty or has non-numeric values")
    order = np.argsort(voltage, kind="stable")
    voltage, power = voltage[order], power[order]
    if power[0] <= 0:
        raise ValueError("The calibration power at the lowest voltage is not positive. "
                         "Powers must be linear (uW or monitor volts above dark), not dBm.")
    keep = np.cumprod(power > 0).astype(bool)     # Stop at the first reading at or below dark
    voltage, power = voltage[keep], np.minimum.accumulate(power[keep])
    falling = np.concatenate([[True], np.diff(power) < 0])   # Drop flat steps: they cannot be inverted
    dropped = int((~keep).sum() + (~falling).sum())
    voltage, power = voltage[falling], power[falling]
    if voltage.size < MIN_CALIBRATION_POINTS:
        raise ValueError(f"The calibration has only {voltage.size} usable points; at least "
                         f"{MIN_CALIBRATION_POINTS} are needed")
    return voltage, power, dropped


def load_calibration(path) -> Calibration:
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    meta = {}
    if lines and lines[0].startswith("# {"):
        meta = json.loads(lines[0][2:])
    body = "\n".join(line for line in lines if not line.startswith("#"))
    data = np.atleast_1d(np.genfromtxt(io.StringIO(body), delimiter=",", names=True, dtype=float))
    names = data.dtype.names or ()
    column = "power" if "power" in names else next((n for n in names if n.startswith("power")), None)
    if "voltage_v" not in names or column is None:
        raise ValueError(f"{path}: needs voltage_v and power columns")
    voltage, power, dropped = monotonic_curve(data["voltage_v"], data[column])
    return Calibration(voltage, power, meta.get("unit", "uW"), meta, dropped)


def save_calibration(path, rows: list[dict], meta: dict):
    """rows: dicts with voltage_v, code, power and monitor_v (power in meta['unit'], above dark)."""
    meta = {"created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **meta}
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        file.write("# " + json.dumps(meta) + "\n")
        file.write("voltage_v,code,power,monitor_v\n")
        for r in rows:
            monitor = "" if r.get("monitor_v") is None else f"{r['monitor_v']:.5f}"
            file.write(f"{r['voltage_v']:.5f},{r['code']},{r['power']:.8g},{monitor}\n")


# --------------------------------------------------------------------------
# Trace to drive
# --------------------------------------------------------------------------

def plan_drive(h, calibration: Calibration, mean_power: float | None = None):
    """Target power = mean x h, clipped to the calibrated range, then inverted to volts.

    Returns (volts, clipped target, statistics). Without a mean power, the
    largest mean that keeps the peaks inside the range is used.
    """
    h = np.asarray(h, dtype=float)
    if h.size == 0 or not np.all(np.isfinite(h)):
        raise ValueError("The trace is empty or has non-finite values")
    top, bottom = calibration.power_max, calibration.power_min
    if mean_power is None:
        mean_power = top / max(float(h.max()), 1e-9)
    if not (bottom < mean_power < top):
        raise ValueError(f"The mean power {mean_power:.4g} {calibration.unit} is outside the playable range "
                         f"{bottom:.4g} to {top:.4g} {calibration.unit}")
    target = mean_power * h
    high, low = target > top, target < bottom
    clipped = np.clip(target, bottom, top)
    stats = {
        "mean_power": float(mean_power), "unit": calibration.unit,
        "power_max": top, "power_min": bottom,
        "clipped_high_fraction": float(high.mean()), "clipped_low_fraction": float(low.mean()),
        "clipped_fraction": float((high | low).mean()),
        "scintillation_index_trace": float(h.var() / h.mean() ** 2),
        "scintillation_index_played": float(clipped.var() / clipped.mean() ** 2),
        "below_0v_transmission_db": 10 * math.log10(float(calibration.power[0]) / mean_power),
    }
    if calibration.unit == "uW":
        stats["mean_dbm"] = float(uw_to_dbm(mean_power))
    return calibration.voltage_for(clipped), clipped, stats
