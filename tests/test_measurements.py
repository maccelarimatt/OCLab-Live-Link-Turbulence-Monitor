from dataclasses import replace

import numpy as np
import pytest
from hantek6254bc import Capture

from oclab.acquisition import HardwareSource, analyse
from oclab.config import Settings, load_settings


def waveform(settings=None):
    settings = settings or Settings()
    n = settings.scope.depth
    volts = np.tile([[1.0], [2.0], [2.0], [0.0]], (1, n))
    return Capture(np.arange(n) / settings.scope.sample_rate, volts,
                   np.full((4, n), 128, dtype=np.uint8), settings.scope.sample_rate)


def test_position_uses_per_sample_ratio_and_rejects_dark_and_clipped_samples():
    settings = Settings()
    capture = waveform()
    capture.volts[2, :1000] = 0
    capture.volts[0, :1000] = 100  # excluded from position, retained in voltage means
    capture.raw[1, 1000:2000] = 227
    summary, preview = analyse(capture, settings)
    assert summary["valid_fraction"] == (4096 - 2000) / 4096
    assert summary["position_x"] == 0.5
    assert summary["position_y"] == 1
    assert summary["wander_rms"] == 0
    assert summary["x_mean_v"] > 1
    assert preview["position_x"][0] is None
    assert len(preview["x_v"]) <= 256


def test_calibration_channel_mapping_and_probe_factors():
    settings = Settings()
    settings.scope.x_channel, settings.scope.sum_channel = 3, 1
    settings.scope.probe_factors = [2, 1, 1, 1]
    settings.calibration.x_offset_v = 1
    settings.calibration.x_scale_mm = 10
    settings.calibration.y_scale_mm = -5
    settings.validate()
    summary, _ = analyse(waveform(), settings)
    assert summary["x_mean_v"] == 2
    assert summary["sum_mean_v"] == 2
    assert summary["position_x"] == 5
    assert summary["position_y"] == -5
    assert summary["position_unit"] == "mm"


def test_voltage_model_does_not_divide_already_normalised_outputs():
    settings = Settings()
    settings.calibration.position_model = "voltage"
    settings.calibration.x_scale_mm = 3
    settings.calibration.y_scale_mm = 4
    summary, _ = analyse(waveform(), settings)
    assert summary["position_x"] == 3
    assert summary["position_y"] == 8


def test_no_signal_returns_null_position_not_nan():
    capture = waveform()
    capture.volts[2] = -1
    summary, preview = analyse(capture, Settings())
    assert summary["position_x"] is None
    assert summary["wander_rms"] is None
    assert summary["valid_fraction"] == 0
    assert all(v is None for v in preview["position_y"])


def test_wander_is_rms_about_centroid_not_distance_from_origin():
    settings = Settings()
    capture = waveform()
    capture.volts[0, ::2] = 3
    summary, _ = analyse(capture, settings)
    assert summary["position_x"] == 1
    assert summary["wander_rms"] == pytest.approx(0.5)


@pytest.mark.parametrize("change", [
    {"volts": np.ones((3, 4096))}, {"volts": np.full((4, 4096), np.nan)},
    {"sample_rate": 12345}, {"time_s": np.arange(4096) * 3.0},
])
def test_invalid_driver_data_rejected(change):
    with pytest.raises(ValueError, match="invalid waveform"):
        analyse(replace(waveform(), **change), Settings())


@pytest.mark.parametrize("text", [
    'mode="automatic"', 'capture_interval_s=nan', '[scope]\nx_channel=2',
    '[calibration]\nx_scale_mm=1.0', '[calibration]\nminimum_sum_v=0.0',
    '[scope]\nsample_rate=123', '[scope]\nprobe_factors=[1,1,0,1]',
    'unknown_option=42',
])
def test_bad_configuration_fails_early(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises((ValueError, TypeError)):
        load_settings(str(path))


def test_example_config_is_valid():
    assert load_settings("config.example.toml").scope.sum_channel == 3


def test_real_adapter_uses_pinned_driver_api(monkeypatch):
    calls = []
    class Scope:
        _position_calibration = (1,)  # Calibration table read from the unit's EEPROM
        _scale_calibration = (1,)

        @classmethod
        def open(cls, config, bus, address):
            calls.append((config, bus, address))
            return cls()
        def capture(self):
            return waveform()
        def close(self):
            calls.append("closed")
    monkeypatch.setattr("oclab.acquisition.Hantek6254BC", Scope)
    settings = Settings()
    settings.scope.bus, settings.scope.address = 1, 8
    source = HardwareSource(settings)
    source.capture()
    source.capture()
    assert len(calls) == 1  # Reuse the claimed interface between captures.
    assert calls[0][0].trigger_sweep == "auto"
    assert calls[0][1:] == (1, 8)
    source.close()
    source.capture()
    assert len(calls) == 3



class FlakyScope:
    """Driver stand-in whose EEPROM calibration read fails for the first `failures` opens."""
    failures = 0
    opened = closed = 0

    @classmethod
    def open(cls, config, bus, address):
        cls.opened += 1
        scope = cls()
        scope._position_calibration = None if cls.opened <= cls.failures else (1,)
        scope._scale_calibration = scope._position_calibration
        return scope

    def capture(self):
        return waveform()

    def close(self):
        FlakyScope.closed += 1


@pytest.fixture
def flaky(monkeypatch):
    monkeypatch.setattr("oclab.acquisition.Hantek6254BC", FlakyScope)
    monkeypatch.setattr("oclab.acquisition.time.sleep", lambda s: None)
    FlakyScope.opened = FlakyScope.closed = 0
    return FlakyScope


def test_scope_reopened_until_zero_level_calibration_loads(flaky):
    flaky.failures = 2
    source = HardwareSource(Settings())
    source.capture()
    assert flaky.opened == 3 and flaky.closed == 2  # The two uncalibrated opens were released


def test_scope_without_calibration_is_never_used(flaky):
    from oclab.acquisition import CALIBRATION_ATTEMPTS, CalibrationUnavailable
    flaky.failures = 99
    source = HardwareSource(Settings())
    with pytest.raises(CalibrationUnavailable):
        source.capture()
    assert flaky.opened == flaky.closed == CALIBRATION_ATTEMPTS
    assert source.scope is None


def test_unused_channel_level_is_reported():
    capture = waveform()
    capture.volts[3] = 1.23  # CH4 not mapped to X/Y/SUM; a shared zero offset shows here
    summary, _ = analyse(capture, Settings())
    assert summary["unused_channel"] == 4 and summary["unused_channel_mean_v"] == pytest.approx(1.23)


def test_channel_with_blank_calibration_entry_counts_as_uncalibrated():
    from oclab.acquisition import calibrated
    class Scope:
        _position_calibration = (1,)
        def _position_pwm(self, channel, vdiv):
            return None if channel == 2 else 0x7100  # Blank EEPROM entry for CH3 at this range
    assert calibrated(Scope(), [1.0] * 4) is False
    Scope._position_pwm = lambda self, channel, vdiv: 0x7100
    assert calibrated(Scope(), [1.0] * 4) is True


def test_detector_saturation_excluded_like_clipping():
    settings = Settings()
    settings.calibration.saturation_v = 4.0
    capture = waveform()
    capture.volts[2, :1000] = 4.03  # SUM above the detector's 4 V limit (scope not clipped)
    capture.volts[0, 2000:2100] = -4.05  # |X| above the limit
    summary, _ = analyse(capture, settings)
    assert summary["saturated_fraction"] == pytest.approx(1100 / 4096)
    assert summary["clipped_fraction"] == 0
    assert summary["valid_fraction"] == pytest.approx(1 - 1100 / 4096)
    assert summary["position_x"] == 0.5  # Only unsaturated samples
    from oclab.acquisition import decimate_capture
    _, extra = decimate_capture(capture, settings)
    assert extra["stored_valid_fraction"] < 1 - 1100 / 4096  # Filter windows touching saturation excluded too
    settings.calibration.saturation_v = None
    assert analyse(capture, settings)[0]["saturated_fraction"] == 0


def test_saturation_must_exceed_minimum_sum(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[calibration]\nsaturation_v = 0.01\n")
    with pytest.raises(ValueError, match="saturation_v"):
        load_settings(str(path))
