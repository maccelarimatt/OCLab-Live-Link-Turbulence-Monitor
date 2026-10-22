import csv
import gzip
import io
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from hantek6254bc import Capture

from oclab.acquisition import DemoSource, analyse, decimate_capture
from oclab.app import create_app
from oclab.config import Settings, load_settings
from oclab.decimation import decimate, fir, plan
from oclab.service import Recorder
from oclab.storage import Store

from test_recording import wait_for


def test_filter_passes_band_and_rejects_aliases():
    fs, t = 25000, np.arange(16384) / 25000
    for f0, expected in ((200, 1), (1000, 1), (1300, 0), (3000, 0), (11000, 0)):
        y = decimate(np.sin(2 * np.pi * f0 * t)[None], 10)[0]
        assert np.sqrt(np.mean(y ** 2) * 2) == pytest.approx(expected, abs=1e-3)  # >= 60 dB down when expected 0
    h = fir(10)
    assert h.sum() == pytest.approx(1) and np.allclose(h, h[::-1])  # Unity DC gain, linear phase


@pytest.mark.parametrize("args,match", [
    ((25000, 3000, 16384), "divide"), ((25000, 50000, 4096), "divide"), ((25000, 100, 4096), "leaves only"),
    ((25000, 0, 4096), "positive"),
])
def test_unusable_store_rates_rejected(args, match):
    with pytest.raises(ValueError, match=match):
        plan(*args)


def test_operating_frequency_from_config_and_command_line(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("capture_interval_s = 0\n[scope]\ndepth = 16384\nstore_rate_hz = 5000\n")
    settings = load_settings(str(path))
    assert settings.scope.store_rate_hz == 5000 and settings.capture_interval_s == 0
    assert load_settings(str(path), store_rate=1250).scope.store_rate_hz == 1250
    assert load_settings(None, store_rate=5000, scope_rate=50000).scope.sample_rate == 50000
    with pytest.raises(ValueError, match="divide"):
        load_settings(str(path), store_rate=3000)
    with pytest.raises(ValueError, match="Capture interval"):
        load_settings(None, capture_interval_s=-1)


def capture(settings, sum_v=2.0):
    n = settings.scope.depth
    volts = np.zeros((4, n))
    volts[2] = sum_v
    return Capture(np.arange(n) / settings.scope.sample_rate, volts, np.full((4, n), 128, dtype=np.uint8), settings.scope.sample_rate)


def test_stored_codes_reproduce_driver_volts_and_flag_clipping():
    s = Settings()
    s.scope.store_rate_hz = 25000  # No decimation: stored values must equal driver volts exactly
    s.scope.volts_per_div = [0.5, 1.0, 2.0, 5.0]
    c = DemoSource(s).capture()
    stored, extra = decimate_capture(c, s)
    assert stored["codes"].dtype == np.int16 and stored["codes"].shape == (4, s.scope.depth)
    volts = stored["codes"] * (np.array(s.scope.volts_per_div) / (31 * 64))[:, None]
    assert np.allclose(volts, c.volts, atol=1e-12)
    assert extra["bandwidth_hz"] == 12500
    s.scope.store_rate_hz = 2500
    c.raw[2, 2000] = 227  # One SUM sample at the ADC rail
    stored, extra = decimate_capture(c, s)
    assert 30 <= stored["clipped"].sum() <= 40  # Every output whose filter touched it
    assert extra["stored_samples"] == stored["codes"].shape[1] == plan(25000, 2500, 4096)["stored"]


def test_scintillation_index_of_known_fluctuation():
    s = Settings()
    s.scope.store_rate_hz = 2500
    c = capture(s)
    t = c.time_s
    c.volts[2] = 2.0 * (1 + 0.1 * np.sin(2 * np.pi * 300 * t))  # In band: variance (0.1^2)/2 of the normalised mean
    _, extra = decimate_capture(c, s)
    assert extra["scintillation_index"] == pytest.approx(0.005, rel=0.02)
    c.volts[2] = 2.0 * (1 + 0.1 * np.sin(2 * np.pi * 5000 * t))  # Above the band: removed by the filter
    _, extra = decimate_capture(c, s)
    assert extra["scintillation_index"] < 1e-6
    c.volts[2] = 0.0  # No light: no index rather than a false value
    assert decimate_capture(c, s)[1]["scintillation_index"] is None


def test_minutes_and_npz_exports(tmp_path):
    settings = Settings(mode="demo", data_dir=str(tmp_path), capture_interval_s=0, retry_interval_s=0.1, minimum_free_disk_mb=0)
    settings.weather.enabled = False
    recorder = Recorder(settings)
    with TestClient(create_app(settings, recorder)) as client:
        wait_for(lambda: recorder.snapshot()["status"]["recording"])
        wait_for(lambda: recorder.store.session(recorder.session_id)["capture_count"] >= 5)
        session_id = recorder.session_id
        recorder.stop()
        info = client.get(f"/api/sessions/{session_id}/metadata.json").json()
        minutes = list(csv.DictReader(io.StringIO(client.get(f"/api/sessions/{session_id}/minutes.csv").text)))
        assert sum(int(m["captures"]) for m in minutes) == info["capture_count"]
        assert all(0 < float(m["coverage_fraction"]) <= 1 for m in minutes)
        response = client.get(f"/api/sessions/{session_id}/session.npz")
        assert response.status_code == 200
        with np.load(io.BytesIO(response.content)) as data:
            n = info["capture_count"]
            m = info["metadata"]["decimation"]["stored"]
            assert data["codes"].shape == (n, 4, m) and data["clipped"].shape == (n, m)
            assert int(data["store_rate_hz"]) == settings.scope.store_rate_hz
            assert data["psd_intensity"].shape == (len(data["psd_minute_start_ns"]), m // 2 + 1)
            assert data["psd_bursts"].sum() <= n
            volts = data["codes"][0] * data["volts_per_count"][:, None]
            first_id = str(data["capture_id"][0])
        raw = list(csv.DictReader(io.StringIO(gzip.decompress(client.get(f"/api/sessions/{session_id}/raw.csv.gz").content).decode())))
        first = [float(r["ch3_v"]) for r in raw if r["capture_id"] == first_id]
        assert np.allclose(first, volts[2])  # NPZ and CSV agree


def test_old_format_sessions_still_export(tmp_path):
    s = Settings(mode="demo", data_dir=str(tmp_path))
    store = Store(str(tmp_path))
    session_id = store.start_session(s)
    c = DemoSource(s).capture()
    summary, _ = analyse(c, s)
    summary.update(source="demo", host_started_ns=1, host_received_ns=2, host_started_utc="x", host_received_utc="y")
    buf = io.BytesIO()
    np.savez_compressed(buf, scope_volts=c.volts, raw=c.raw, time_s=c.time_s)  # Schema 1-2 blob
    with store.connect() as db:
        metadata = json.loads(db.execute("SELECT metadata FROM sessions").fetchone()[0])
        metadata["schema_version"] = 2
        metadata.pop("decimation")
        db.execute("UPDATE sessions SET metadata=?", (json.dumps(metadata),))
        db.execute("INSERT INTO captures(session_id,received_ns,summary,waveform) VALUES(?,?,?,?)",
                   (session_id, 2, json.dumps({**summary, "session_id": session_id}), buf.getvalue()))
    client = TestClient(create_app(s, Recorder(s)))
    rows = list(csv.DictReader(io.StringIO(gzip.decompress(client.get(f"/api/sessions/{session_id}/raw.csv.gz").content).decode())))
    assert len(rows) == s.scope.depth and "ch4_raw" in rows[0] and float(rows[1]["sample_time_s"]) == 1 / 25000
    assert client.get(f"/api/sessions/{session_id}/session.npz").status_code == 409


def test_oversized_npz_refused(tmp_path, monkeypatch):
    s = Settings(mode="demo", data_dir=str(tmp_path))
    recorder = Recorder(s)
    session_id = recorder.store.start_session(s)
    monkeypatch.setattr(Store, "npz_size", staticmethod(lambda session: 10 ** 10))
    response = TestClient(create_app(s, recorder)).get(f"/api/sessions/{session_id}/session.npz")
    assert response.status_code == 413 and "raw.csv.gz" in response.json()["detail"]
