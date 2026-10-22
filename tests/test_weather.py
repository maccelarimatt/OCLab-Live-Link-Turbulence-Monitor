import csv
import io
import os
import time

import pytest
from fastapi.testclient import TestClient

from oclab.acquisition import DemoSource
from oclab.app import create_app
from oclab.config import Settings, load_settings
from oclab.service import Recorder
from oclab.storage import Store, ns_to_utc
from oclab.weather import ParseError, SerialWeatherSource, WeatherRecorder, associate, parse_header, parse_line

HEADER = "# pico-weather v2.1 wind=10Hz anemo=4-20mA shunt=165.0ohm range=30.0m/s light=ldr baro=BMP280 dht=yes"
S = 1_000_000_000


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for recorder")


class LineSource:
    """Fake serial source: each connection plays one script; an exception item simulates unplugging."""

    port = "/dev/ttyFAKE"

    def __init__(self, *connections):
        self.connections = list(connections)
        self.opened = 0
        self.lines = iter(())

    def open(self):
        self.opened += 1
        if not self.connections:
            raise OSError("No Pico (USB VID 0x2E8A) serial port found")
        script = self.connections.pop(0)
        if isinstance(script, Exception):
            raise script
        self.lines = iter(script)

    def read_line(self):
        item = next(self.lines, None)
        if item is None:
            time.sleep(0.02)
            return None
        if isinstance(item, Exception):
            raise item
        time.sleep(0.002)  # Distinct, increasing host timestamps.
        return item + "\n"

    def close(self):
        pass


def settings(tmp_path, **weather):
    s = Settings(mode="demo", data_dir=str(tmp_path), capture_interval_s=0.1, retry_interval_s=0.1, minimum_free_disk_mb=0)
    for key, value in weather.items():
        setattr(s.weather, key, value)
    return s


def rows(store, table):
    with store.connect() as db:
        return [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY id")]


# Parsing -------------------------------------------------------------------

def test_parse_valid_wind_and_environment_lines():
    assert parse_line("W,123456,0.8251,5.001,0.002,0\r\n") == ("W", {"t_ms": 123456, "v_adc": 0.8251, "i_ma": 5.001, "wind_ms": 0.002, "clip": 0})
    assert parse_line("E,123500,19,41,835.21,18.62,1.604,10574") == ("E", {
        "t_ms": 123500, "dht_c": 19.0, "dht_rh": 41.0, "baro_hpa": 835.21, "baro_c": 18.62, "light_v": 1.604, "light_x": 10574.0})


def test_empty_fields_are_null_never_zero():
    kind, fields = parse_line("E,10,,,835.2,,1.6,")
    assert kind == "E"
    assert fields["dht_c"] is None and fields["dht_rh"] is None and fields["baro_c"] is None and fields["light_x"] is None
    assert fields["baro_hpa"] == 835.2
    assert parse_line("W,10,,,,") == ("W", {"t_ms": 10, "v_adc": None, "i_ma": None, "wind_ms": None, "clip": None})
    assert parse_line("W,10,0.1,4.0,nan,0")[1]["wind_ms"] is None  # Non-finite never reaches JSON


@pytest.mark.parametrize("line", [
    "", "   ", "W,123,0.8,5.0", "W,123,0.8,5.0,0.1,0,9", "E,1,2", "2,0.8,5.0,0.1,0", "51,0.8,5.0,0.1,0",
    "X,1,2,3", "W,,0.8,5.0,0.1,0", "W,-5,0.8,5.0,0.1,0", "W,1,0.8,abc,0.1,0", "W,1,0.8,5.0,0.1,2",
    "W,1.5,0.8,5.0,0.1,0", "E,1,19,41,835.2,18.6,1.6,1.2.3", "\x00\x01garbage", "W,1,0.8,5.0,0.1",
])
def test_malformed_and_partial_lines_raise(line):
    with pytest.raises(ParseError):
        parse_line(line)


def test_header_and_comment_lines():
    kind, header = parse_line(HEADER)
    assert kind == "header"
    assert header == {"firmware": "pico-weather", "version": "v2.1", "wind": "10Hz", "anemo": "4-20mA", "shunt": "165.0ohm",
                      "range": "30.0m/s", "light": "ldr", "baro": "BMP280", "dht": "yes"}
    assert parse_line("# DHT11 read failed") == ("comment", "# DHT11 read failed")
    assert parse_header("# light=bpw34") == {"light": "bpw34"}


# Association maths ---------------------------------------------------------------

def wind_at(ns, speed):
    return {"host_received_ns": ns, "wind_ms": speed}


def env_at(ns, **values):
    base = dict(dht_c=19.0, dht_rh=40.0, baro_hpa=835.0, baro_c=18.5, light_v=1.6, light_x=10000.0)
    return {"host_received_ns": ns, **base, **values}


def test_association_window_edges_and_statistics():
    end = 1000 * S
    wind = [wind_at(end - 10 * S, 100.0),  # Exactly at the window start: excluded
            wind_at(end - 9 * S, 2.0), wind_at(end - 5 * S, None), wind_at(end - 1 * S, 4.0),
            wind_at(end, 6.0),  # Exactly at the capture: included
            wind_at(end + 1, 100.0)]  # After the capture: excluded
    result = associate(wind, [env_at(end - 2 * S)], end, 10, 5)
    assert result["wind_n"] == 3  # Null wind excluded, not counted as zero
    assert result["wind_mean_ms"] == pytest.approx(4.0)
    assert result["wind_std_ms"] == pytest.approx((8 / 3) ** 0.5)
    assert result["wind_max_ms"] == 6.0
    assert result["wind_ti"] == pytest.approx((8 / 3) ** 0.5 / 4)
    assert result["taylor_ok"] is True
    assert result["air_temp_c"] == 18.5 and result["dht_temp_c"] == 19.0 and result["rh_pct"] == 40.0
    assert result["pressure_hpa"] == 835.0 and result["ldr_ohm"] == 10000.0
    assert result["weather_age_s"] == pytest.approx(2.0)
    assert result["weather_status"] == "ok"


def test_association_with_no_samples_is_missing_not_zero():
    result = associate([], [], 1000 * S, 10, 5)
    assert result["weather_status"] == "missing"
    assert result["wind_n"] == 0
    assert all(result[k] is None for k in ("wind_mean_ms", "wind_std_ms", "wind_ti", "taylor_ok", "air_temp_c", "weather_age_s"))


def test_association_stale_and_low_wind():
    end = 1000 * S
    # Wind fresh, environment 30 s old: values are reported with their age but marked stale.
    result = associate([wind_at(end - S, 0.5), wind_at(end, 0.7)], [env_at(end - 30 * S)], end, 10, 5)
    assert result["weather_status"] == "stale"
    assert result["weather_age_s"] == pytest.approx(30)
    assert result["wind_ti"] is None  # Below 1 m/s
    assert result["taylor_ok"] is False
    # Environment fresh, last wind 8 s old.
    assert associate([wind_at(end - 8 * S, 3.0)], [env_at(end)], end, 10, 5)["weather_status"] == "stale"
    # Environment received after the capture is not used.
    assert associate([], [env_at(end + 1)], end, 10, 5)["weather_status"] == "missing"
    # High turbulence intensity fails the Taylor check.
    assert associate([wind_at(end - S, 1.0), wind_at(end, 4.0)], [env_at(end)], end, 10, 5)["taylor_ok"] is False


# Serial source ----------------------------------------------------------------------

def test_serial_source_autodetects_pico_and_reassembles_partial_lines(monkeypatch):
    import serial
    from serial.tools import list_ports

    class Port:
        def __init__(self, device, vid):
            self.device, self.vid = device, vid
    monkeypatch.setattr(list_ports, "comports", lambda: [Port("/dev/ttyUSB0", 0x0403), Port("/dev/ttyACM1", 0x2E8A)])
    opened = []

    class FakeSerial:
        chunks = [b"W,1,0.8,", b"", b"5.0,0.1,0\n", b"E,2,19,41,835.2,18.6,1.6,9000\r\n"]
        def __init__(self, port, baud, timeout):
            opened.append((port, baud))
        def readline(self):
            return self.chunks.pop(0) if self.chunks else b""
        def close(self):
            opened.append("closed")
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    source = SerialWeatherSource(Settings())
    source.open()
    assert opened == [("/dev/ttyACM1", 115200)]
    assert source.read_line() is None  # Partial line kept until its newline
    assert source.read_line() is None
    assert source.read_line() == "W,1,0.8,5.0,0.1,0\n"
    assert source.read_line().strip() == "E,2,19,41,835.2,18.6,1.6,9000"
    source.close()
    monkeypatch.setattr(list_ports, "comports", lambda: [Port("/dev/ttyUSB0", 0x0403)])
    with pytest.raises(OSError, match="0x2E8A"):
        source.open()


def test_pty_serial_stream_end_to_end(tmp_path):
    master, slave = os.openpty()
    try:
        s = settings(tmp_path, port=os.ttyname(slave))
        s.mode = "hardware"
        store = Store(str(tmp_path))
        weather = WeatherRecorder(s, store, SerialWeatherSource(s))
        weather.start()
        try:
            wait_for(lambda: weather.snapshot()["state"] == "connected")
            os.write(master, (HEADER + "\r\nW,100,0.8,5.0,1.25,0\r\nW,200,0.8,5.0\r\n" "E,300,,41,835.2,18.6,1.6,9000\r\n").encode())
            wait_for(lambda: weather.snapshot()["counters"]["env"] == 1)
            wait_for(lambda: len(rows(store, "env_samples")) == 1)
        finally:
            assert weather.stop()
        snap = weather.snapshot()
        assert snap["counters"]["malformed"] == 1 and snap["counters"]["wind"] == 1
        assert snap["header"]["shunt"] == "165.0ohm"
        assert rows(store, "wind_samples")[0]["wind_ms"] == 1.25
        env = rows(store, "env_samples")[0]
        assert env["dht_c"] is None and env["source"] == "hardware" and env["raw"] == "E,300,,41,835.2,18.6,1.6,9000"
    finally:
        os.close(master)
        os.close(slave)


# Recorder integration -------------------------------------------------------------------

def test_fake_source_end_to_end_with_captures(tmp_path):
    lines = [HEADER, "W,1,0.8", "garbage"] + [f"W,{i},0.8,5.0,{2 + i % 3},0" for i in range(400)]
    lines[200:200] = ["E,999,19,,835.2,18.6,1.6,9000"]
    s = settings(tmp_path)
    recorder = Recorder(s, weather_source=LineSource(lines))
    recorder.start()
    try:
        wait_for(lambda: recorder.weather.snapshot()["counters"]["wind"] == 400)
        wait_for(lambda: (recorder.snapshot()["latest"] or {}).get("summary", {}).get("weather_status") == "ok")
        summary = recorder.snapshot()["latest"]["summary"]
        assert summary["wind_n"] > 0 and 2 <= summary["wind_mean_ms"] <= 4
        assert summary["rh_pct"] is None and summary["air_temp_c"] == 18.6
        assert "weather" in recorder.snapshot()
        session_id = recorder.session_id
    finally:
        recorder.stop()
    session = recorder.store.session(session_id)
    counters = recorder.weather.snapshot()["counters"]
    assert counters["malformed"] == 2 and counters["headers"] == 1
    assert len(rows(recorder.store, "wind_samples")) == 400  # Flushed on stop
    assert rows(recorder.store, "env_samples")[0]["dht_rh"] is None
    assert len(rows(recorder.store, "weather_headers")) == 1
    meta = session["metadata"]
    assert meta["schema_version"] == 2
    assert meta["weather"]["header"]["firmware"] == "pico-weather"
    assert meta["weather"]["association"]["window_s"] == 10
    assert any("Cn2" in c for c in meta["weather"]["caveats"])


def test_hardware_mode_never_fabricates_weather(tmp_path):
    s = settings(tmp_path)
    s.mode = "hardware"
    # Real scope stand-in; the weather station is absent.
    recorder = Recorder(s, source=DemoSource(s), weather_source=LineSource())
    recorder.start()
    try:
        wait_for(lambda: recorder.snapshot()["latest"] is not None)
        wait_for(lambda: recorder.weather.snapshot()["state"] == "disconnected")
        summary = recorder.snapshot()["latest"]["summary"]
        assert summary["weather_status"] == "missing"
        assert summary["wind_mean_ms"] is None and summary["air_temp_c"] is None
        assert recorder.snapshot()["status"]["recording"]  # Scope keeps recording
    finally:
        recorder.stop()
    assert rows(recorder.store, "wind_samples") == []
    assert rows(recorder.store, "env_samples") == []


def test_demo_mode_uses_labelled_synthetic_weather(tmp_path):
    recorder = Recorder(settings(tmp_path))
    recorder.start()
    try:
        wait_for(lambda: recorder.weather.snapshot()["counters"]["env"] >= 1, timeout=5)
        wait_for(lambda: len(rows(recorder.store, "wind_samples")) > 0)
        snap = recorder.weather.snapshot()
        assert snap["source"] == "demo" and snap["port"] == "demo" and snap["header"]["version"] == "demo"
    finally:
        recorder.stop()
    assert {r["source"] for r in rows(recorder.store, "wind_samples")} == {"demo"}


def test_disabled_weather_records_nothing(tmp_path):
    recorder = Recorder(settings(tmp_path, enabled=False))
    recorder.start()
    try:
        wait_for(lambda: recorder.snapshot()["latest"] is not None)
        assert recorder.snapshot()["latest"]["summary"]["weather_status"] == "disabled"
        assert recorder.weather.snapshot()["state"] == "disabled"
        assert recorder.weather.thread is None
    finally:
        recorder.stop()


def test_usb_serial_disconnect_and_reconnect(tmp_path):
    first = [HEADER] + [f"W,{i},0.8,5.0,3.0,0" for i in range(20)] + [OSError("device reports readiness to read but returned no data")]
    second = [f"W,{i},0.8,5.0,4.0,0" for i in range(4294967290, 4294967300)]  # t_ms near a wrap, kept as-is
    source = LineSource(first, OSError("could not open port /dev/ttyACM0"), second)
    s = settings(tmp_path)
    s.mode = "hardware"
    store = Store(str(tmp_path))
    weather = WeatherRecorder(s, store, source)
    weather.start()
    try:
        wait_for(lambda: weather.snapshot()["counters"]["wind"] == 30)
        assert weather.snapshot()["state"] == "receiving"
        assert weather.snapshot()["wind"]["wind_ms"] == 4.0
    finally:
        assert weather.stop()
    assert source.opened == 3
    assert weather.snapshot()["counters"]["connection_errors"] == 2
    assert len(rows(store, "wind_samples")) == 30  # Nothing lost across the reconnect


def test_silent_port_reports_firmware_not_running(tmp_path):
    weather = WeatherRecorder(settings(tmp_path), Store(str(tmp_path)), LineSource([], []))
    weather.silence_limit_s = 0.2
    weather.start()
    try:
        wait_for(lambda: weather.snapshot()["counters"]["connection_errors"] >= 1)
        assert "main.py" in weather.snapshot()["message"]
    finally:
        assert weather.stop()


def test_failed_weather_write_keeps_samples_for_retry(tmp_path, monkeypatch):
    s = settings(tmp_path)
    store = Store(str(tmp_path))
    weather = WeatherRecorder(s, store, LineSource())
    original = store.save_weather
    monkeypatch.setattr(store, "save_weather", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    weather.handle("W,1,0.8,5.0,3.0,0", time.time_ns(), time.monotonic())
    weather.flush()
    assert weather.snapshot()["state"] == "storage_error" and weather.snapshot()["counters"]["write_errors"] == 1
    monkeypatch.setattr(store, "save_weather", original)
    weather.flush()
    assert len(rows(store, "wind_samples")) == 1
    assert weather.snapshot()["state"] == "receiving"


# Export and API ------------------------------------------------------------------------

def test_weather_csv_export_count_and_boundaries(tmp_path):
    s = settings(tmp_path)
    s.mode = "hardware"
    store = Store(str(tmp_path))
    start = 1_800_000_000 * S
    end = start + 60 * S
    wind = [{"host_received_ns": start + k * S // 10, "t_ms": k, "v_adc": 0.8, "i_ma": 5.0, "wind_ms": None if k == 5 else 2.0,
             "clip": 0, "raw": f"W,{k},..."} for k in range(-101, 602)]  # 10 Hz from start-10.1 s to end+0.1 s
    env = [{"host_received_ns": start + k * S, "t_ms": k, "dht_c": None, "dht_rh": 40.0, "baro_hpa": 835.0, "baro_c": 18.0,
            "light_v": 1.6, "light_x": 9000.0, "raw": f"E,{k},..."} for k in range(-11, 62)]
    store.save_weather("hardware", wind, env)
    store.save_weather("demo", wind[:50], [])  # Other source: never mixed into a hardware export
    session_id = store.start_session(s)
    with store.connect() as db:
        db.execute("UPDATE sessions SET started_utc=?, ended_utc=?, status='complete' WHERE id=?",
                   (ns_to_utc(start), ns_to_utc(end), session_id))
    client = TestClient(create_app(s, Recorder(s)))  # Lifespan not started: no acquisition
    response = client.get(f"/api/sessions/{session_id}/weather.csv")
    assert response.status_code == 200
    assert int(response.headers["X-Export-Weather-From-NS"]) == start - 10 * S
    assert int(response.headers["X-Export-Weather-Through-NS"]) == end
    data = list(csv.DictReader(io.StringIO(response.text)))
    times = [int(r["host_received_ns"]) for r in data]
    assert times == sorted(times)
    assert min(times) == start - 10 * S and max(times) == end  # Both boundaries inclusive
    assert sum(r["kind"] == "W" for r in data) == 701  # -100..600 tenths of a second
    assert sum(r["kind"] == "E" for r in data) == 71  # -10..60 s
    assert {r["source"] for r in data} == {"hardware"}
    w5 = next(r for r in data if r["kind"] == "W" and r["pico_t_ms"] == "5")
    assert w5["wind_ms"] == "" and w5["dht_c"] == ""  # Nulls are empty, never 0
    e0 = next(r for r in data if r["kind"] == "E" and r["pico_t_ms"] == "0")
    assert e0["dht_c"] == "" and e0["dht_rh"] == "40.0" and e0["wind_ms"] == ""
    # Batch boundaries: more rows than one 2000-row batch, nothing duplicated or lost.
    many = list(csv.DictReader(io.StringIO(b"".join(store.weather_csv("hardware", start - 20 * S, end + S)).decode())))
    assert len(many) == len(wind) + len(env)
    store.save_weather("hardware", [dict(wind[0], host_received_ns=start + 100 * S + k) for k in range(2500)], [])
    assert len(list(csv.DictReader(io.StringIO(b"".join(store.weather_csv("hardware", start, end + 200 * S)).decode())))) == 602 + 62 + 2500  # k >= 0
    assert client.get("/api/sessions/nonexistent/weather.csv").status_code == 404


def test_weather_api_and_summary_csv(tmp_path):
    s = settings(tmp_path)
    recorder = Recorder(s)
    with TestClient(create_app(s, recorder)) as client:
        wait_for(lambda: recorder.snapshot()["status"]["recording"])
        wait_for(lambda: len(rows(recorder.store, "wind_samples")) > 0)
        live = client.get("/api/weather/live").json()
        assert live["enabled"] and live["source"] == "demo" and "counters" in live and "raw" not in (live["wind"] or {})
        history = client.get("/api/weather/history?seconds=60").json()
        assert history["wind"] and history["seconds"] == 60
        assert client.get("/api/weather/history?seconds=0").status_code == 422
        assert client.get("/api/weather/history?seconds=3601").status_code == 422
        health = client.get("/api/health")
        assert health.status_code == 200 and health.json()["weather"]["enabled"] is True
        assert "weather" in client.get("/api/live").json()
        assert client.get("/api/config").json()["weather"]["baud"] == 115200
        session_id = recorder.snapshot()["status"]["session_id"]
        recorder.stop()
        summary = list(csv.DictReader(io.StringIO(client.get(f"/api/sessions/{session_id}/summary.csv").text)))
        assert list(summary[0])[-14:] == ["wind_mean_ms", "wind_std_ms", "wind_max_ms", "wind_n", "wind_ti", "taylor_ok",
                                          "air_temp_c", "dht_temp_c", "rh_pct", "pressure_hpa", "light_v", "ldr_ohm",
                                          "weather_age_s", "weather_status"]
        assert list(summary[0])[:3] == ["capture_id", "session_id", "source"]
        weather = list(csv.DictReader(io.StringIO(client.get(f"/api/sessions/{session_id}/weather.csv").text)))
        assert weather and {r["source"] for r in weather} == {"demo"}


@pytest.mark.parametrize("text", [
    "[weather]\nbaud=0", "[weather]\nassociation_window_s=0", "[weather]\nstale_after_s=nan",
    '[weather]\nenabled="yes"', '[weather]\nport=""', "[weather]\nunknown=1",
])
def test_bad_weather_configuration_fails_early(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises((ValueError, TypeError)):
        load_settings(str(path))


def test_example_config_weather_defaults():
    w = load_settings("config.example.toml").weather
    assert (w.enabled, w.port, w.baud, w.association_window_s, w.stale_after_s) == (True, None, 115200, 10.0, 5.0)
