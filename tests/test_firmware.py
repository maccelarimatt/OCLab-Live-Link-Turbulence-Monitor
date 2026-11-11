"""The Pico firmware's pure calculations, imported under CPython with a stub `machine` module."""
import importlib.util
from pathlib import Path
import sys
import types

import pytest

from oclab.config import Settings
from oclab.storage import Store
from oclab.weather import WeatherRecorder, parse_line

from test_weather import LineSource


@pytest.fixture(scope="module")
def fw():
    stub = types.ModuleType("machine")
    stub.ADC = stub.I2C = stub.Pin = object
    sys.modules["machine"] = stub
    try:
        spec = importlib.util.spec_from_file_location("pico_main", Path(__file__).parents[1] / "firmware/pico_weather/main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # main() is not run: guarded by __name__
        return module
    finally:
        del sys.modules["machine"]


def test_rest_level_reads_zero_and_light_wind_is_not_lost(fw):
    assert fw.ANEMO_ZERO_MA == 3.72
    i_ma, wind = fw.wind_from_volts(0.614)  # Measured with the cups at rest
    assert i_ma == pytest.approx(3.721, abs=0.001) and wind == pytest.approx(0, abs=0.01)
    assert fw.wind_from_volts(0.605)[1] == 0  # ADC noise below rest clamps to zero, not negative
    # 0.5 m/s above rest: read as 0 with the old fixed 4.00 mA zero
    v = (3.72 + 0.5 / 30 * 16) * 165 / 1000
    assert fw.wind_from_volts(v)[1] == pytest.approx(0.5, abs=0.01)
    assert fw.wind_from_volts((3.72 + 16) * 0.165)[1] == pytest.approx(30.0)


def test_open_loop_gives_no_wind_value(fw):
    assert fw.wind_from_volts(0.010) == (pytest.approx(0.0606, abs=0.001), None)  # Unplugged: 0.010 V
    assert fw.wind_from_volts(0.52)[1] is None  # 3.15 mA
    assert fw.wind_from_volts(0.54)[1] == 0     # 3.27 mA: a dip below rest, still a live loop


def test_frozen_signal_is_flagged_but_rest_and_real_wind_are_not(fw):
    import random
    rng = random.Random(1)
    def feed(level, spread):
        check, out = fw.FrozenCheck(), []
        for _ in range(fw.FROZEN_WINDOW * 2):
            out.append(check.add(level + rng.gauss(0, spread)))
        return [w for w in out if w]
    frozen = feed(1.525, 0.003)  # The stuck level of 22 Oct 2026
    assert len(frozen) == 2 and "wind_frozen level=9.24mA" in frozen[0]
    assert parse_line(frozen[0])[0] == "comment"  # A valid protocol line
    assert feed(0.614, 0.003) == []  # Cups at rest: flat, but at the zero level
    assert feed(1.525, 0.05) == []   # Real wind fluctuates
    assert len(frozen[0]) < 200


def test_header_reports_the_zero(fw):
    kind, header = parse_line(fw.header(None, None, 21))
    assert kind == "header" and header["version"] == "v3.2" and header["zero"] == "3.72mA" and header["range"] == "30.0m/s"


def test_app_surfaces_firmware_warnings(tmp_path):
    import time
    s = Settings(mode="demo", data_dir=str(tmp_path))
    weather = WeatherRecorder(s, Store(str(tmp_path)), LineSource())
    weather.handle("# note: something informational", time.time_ns(), time.monotonic())
    assert weather.snapshot()["warning"] is None
    weather.handle("# warning wind_frozen level=9.24mA sd=0.0030V over 60s: sensor not measuring", time.time_ns(), time.monotonic())
    assert weather.snapshot()["warning"].startswith("wind_frozen level=9.24mA")
    weather.warning = (weather.warning[0], time.monotonic() - 200)  # Firmware stopped repeating it
    assert weather.snapshot()["warning"] is None
