from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
import tomllib

from hantek6254bc import ScopeConfig


@dataclass
class ScopeSettings:
    sample_rate: int = 25_000
    depth: int = 4096
    volts_per_div: list[float] = field(default_factory=lambda: [1.0] * 4)
    probe_factors: list[float] = field(default_factory=lambda: [1.0] * 4)
    x_channel: int = 1
    y_channel: int = 2
    sum_channel: int = 3
    bus: int | None = None
    address: int | None = None

    def driver_config(self) -> ScopeConfig:
        return ScopeConfig(sample_rate=self.sample_rate, depth=self.depth,
                           volts_per_div=tuple(self.volts_per_div), trigger_sweep="auto")


@dataclass
class Calibration:
    x_offset_v: float = 0.0
    y_offset_v: float = 0.0
    sum_offset_v: float = 0.0
    minimum_sum_v: float = 0.05
    position_model: str = "difference_over_sum"
    x_scale_mm: float | None = None
    y_scale_mm: float | None = None
    notes: str = "Position uncalibrated. Verify voltages against a known reference."


@dataclass
class Experiment:
    title: str = "Live laser link"
    institution: str = "University of the Witwatersrand"
    laboratory: str = "Optical Communications Lab"
    laser: str = "CW532-005"
    wavelength_nm: float = 532.0
    one_way_distance_m: float = 150.0
    detector: str = "PDP90A"
    location: str = "Johannesburg, South Africa"
    data_license: str = "Not specified — contact the lab for reuse terms"
    contact: str = ""


@dataclass
class WeatherSettings:
    enabled: bool = True
    port: str | None = None                # None auto-detects the Pico by USB VID 0x2E8A
    baud: int = 115200
    association_window_s: float = 10.0
    stale_after_s: float = 5.0


@dataclass
class Settings:
    mode: str = "hardware"
    data_dir: str = "./data"
    host: str = "127.0.0.1"
    port: int = 8000
    capture_interval_s: float = 0.5
    session_duration_s: float = 3600
    minimum_free_disk_mb: float = 1024
    retry_interval_s: float = 5
    scope: ScopeSettings = field(default_factory=ScopeSettings)
    calibration: Calibration = field(default_factory=Calibration)
    experiment: Experiment = field(default_factory=Experiment)
    weather: WeatherSettings = field(default_factory=WeatherSettings)

    def validate(self) -> None:
        # Reject NaN/Inf from TOML before they can reach scientific calculations/JSON.
        def finite(value):
            if isinstance(value, dict):
                return all(finite(v) for v in value.values())
            if isinstance(value, list):
                return all(finite(v) for v in value)
            return not isinstance(value, float) or math.isfinite(value)
        if not finite(asdict(self)):
            raise ValueError("Settings must contain only finite numbers")
        if self.mode not in ("hardware", "demo"):
            raise ValueError("mode must be hardware or demo")
        self.scope.driver_config()
        channels = (self.scope.x_channel, self.scope.y_channel, self.scope.sum_channel)
        if len(set(channels)) != 3 or any(type(c) is not int or c not in range(1, 5) for c in channels):
            raise ValueError("X, Y and SUM must use three distinct channels from 1 to 4")
        if len(self.scope.probe_factors) != 4 or any(p <= 0 for p in self.scope.probe_factors):
            raise ValueError("Provide four positive probe_factors")
        if self.capture_interval_s < 0.1 or self.session_duration_s < 1 or self.retry_interval_s < 0.1:
            raise ValueError("Capture/retry interval must be >= 0.1 s; session duration >= 1 s")
        if self.minimum_free_disk_mb < 0 or not 1 <= self.port <= 65535:
            raise ValueError("Invalid disk threshold or port")
        c = self.calibration
        if c.minimum_sum_v <= 0 or c.position_model not in ("difference_over_sum", "voltage"):
            raise ValueError("Use a positive minimum_sum_v and a supported position_model")
        if (c.x_scale_mm is None) != (c.y_scale_mm is None):
            raise ValueError("Set both position scales, or neither")
        if c.x_scale_mm == 0 or c.y_scale_mm == 0:
            raise ValueError("Position scales must be nonzero (negative reverses the axis)")
        if self.experiment.one_way_distance_m <= 0 or self.experiment.wavelength_nm <= 0:
            raise ValueError("Distance and wavelength must be positive")
        w = self.weather
        if type(w.enabled) is not bool or (w.port is not None and (type(w.port) is not str or not w.port)):
            raise ValueError("weather.enabled must be true/false and weather.port a device path")
        if type(w.baud) is not int or w.baud <= 0:
            raise ValueError("weather.baud must be a positive integer")
        if not 1 <= w.association_window_s <= 3600 or not 0.5 <= w.stale_after_s <= 3600:
            raise ValueError("weather.association_window_s must be 1-3600 s; stale_after_s 0.5-3600 s")

    def public(self) -> dict:
        data = asdict(self)
        for key in ("data_dir", "host", "port"):
            data.pop(key)
        return data


def load_settings(path: str | None = None, **overrides) -> Settings:
    data = {}
    if path:
        with Path(path).open("rb") as stream:
            data = tomllib.load(stream)
    for name, cls in (("scope", ScopeSettings), ("calibration", Calibration), ("experiment", Experiment),
                      ("weather", WeatherSettings)):
        if name in data:
            data[name] = cls(**data[name])
    data.update({k: v for k, v in overrides.items() if v is not None})
    settings = Settings(**data)
    settings.validate()
    return settings
