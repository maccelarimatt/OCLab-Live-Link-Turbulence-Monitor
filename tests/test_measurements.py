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

