import csv
import gzip
import io
import sqlite3
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from oclab.acquisition import DemoSource
from oclab.app import create_app
from oclab.config import Settings
from oclab.service import Recorder
from oclab.storage import Store


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for recorder")


def setup(tmp_path):
    settings = Settings(mode="demo", data_dir=str(tmp_path), capture_interval_s=0.1,
                        retry_interval_s=0.1, minimum_free_disk_mb=0)
    return settings, Recorder(settings)


def test_recording_api_raw_export_and_restart(tmp_path):
    settings, recorder = setup(tmp_path)
    with TestClient(create_app(settings, recorder)) as client:
        wait_for(lambda: recorder.snapshot()["status"]["recording"])
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert "data_dir" not in client.get("/api/config").json()
        assert client.get("/api/health").status_code == 200
        snapshot = client.get("/api/live").json()
        assert snapshot["status"]["mode"] == "demo"
        session_id = snapshot["status"]["session_id"]
        # Stop acquisition while leaving the API running, making exact export comparisons possible.
        recorder.stop()
        info = client.get(f"/api/sessions/{session_id}/metadata.json").json()
        assert info["status"] == "complete"
        assert info["metadata"]["settings"]["scope"]["x_channel"] == 1
        summary = client.get(f"/api/sessions/{session_id}/summary.csv")
        summaries = list(csv.DictReader(io.StringIO(summary.text)))
        assert len(summaries) == info["capture_count"]
        assert summaries[0]["source"] == "demo"
        assert int(summary.headers["X-Export-Through-Capture-ID"]) == info["export_through_capture_id"]
        raw = client.get(f"/api/sessions/{session_id}/raw.csv.gz")
        rows = list(csv.DictReader(io.StringIO(gzip.decompress(raw.content).decode())))
        assert len(rows) == info["sample_count"]  # Stored (decimated) samples
        decimation = info["metadata"]["decimation"]
        assert float(rows[0]["sample_time_s"]) == pytest.approx(decimation["t0_s"])
        assert float(rows[1]["sample_time_s"]) - float(rows[0]["sample_time_s"]) == pytest.approx(1 / settings.scope.store_rate_hz)
        first_capture = [r for r in rows if r["capture_id"] == summaries[0]["capture_id"]]
        assert len(first_capture) == decimation["stored"] == int(summaries[0]["stored_samples"])
        # Low-pass filtering keeps the burst mean (unity DC gain); edges are not stored.
        assert np.mean([float(r["ch1_v"]) for r in first_capture]) == pytest.approx(float(summaries[0]["x_mean_v"]), abs=0.005)
        assert all(r["clipped"] == "0" for r in rows)
        assert client.get("/api/sessions/nonexistent/summary.csv").status_code == 404
        assert client.get("/api/history?limit=10000000").status_code == 422
        assert client.post("/api/live").status_code == 405
    store = Store(str(tmp_path))
    assert store.session(session_id)["capture_count"] == len(summaries)
    with store.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_export_snapshot_does_not_grow_during_recording(tmp_path):
    settings, recorder = setup(tmp_path)
    recorder.start()
    try:
        wait_for(lambda: recorder.snapshot()["status"]["recording"])
        info = recorder.store.session(recorder.session_id)
        wait_for(lambda: recorder.store.session(recorder.session_id)["capture_count"] > info["capture_count"])
        data = b"".join(recorder.store.summary_csv(info)).decode()
        assert len(list(csv.DictReader(io.StringIO(data)))) == info["capture_count"]
    finally:
        recorder.stop()


def test_device_failure_reconnects_without_inventing_measurements(tmp_path):
    settings, recorder = setup(tmp_path)
    demo = DemoSource(settings)
    class Source:
        attempts = 0
        closed = 0
        def capture(self):
            self.attempts += 1
            if self.attempts <= 2:
                raise RuntimeError("USB disconnected")
            return demo.capture()
        def close(self):
            self.closed += 1
    recorder.source = Source()
    recorder.start()
    try:
        wait_for(lambda: recorder.snapshot()["status"]["state"] == "disconnected")
        assert recorder.snapshot()["latest"] is None
        assert recorder.store.sessions()["total"] == 0
        wait_for(lambda: recorder.snapshot()["status"]["recording"])
        assert recorder.source.closed >= 2
    finally:
        recorder.stop()


def test_failed_save_is_never_published(tmp_path, monkeypatch):
    settings, recorder = setup(tmp_path)
    def fail(*args):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(recorder.store, "save", fail)
    recorder.start()
    try:
        wait_for(lambda: recorder.snapshot()["status"]["state"] == "storage_error")
        assert recorder.snapshot()["latest"] is None
        assert recorder.snapshot()["status"]["recording"] is False
        assert recorder.store.history() == []
    finally:
        recorder.stop()


def test_low_disk_pauses_before_acquisition(tmp_path, monkeypatch):
    settings, recorder = setup(tmp_path)
    settings.minimum_free_disk_mb = 100
    class Usage:
        free = 1
    monkeypatch.setattr("oclab.service.shutil.disk_usage", lambda _: Usage())
    recorder.start()
    try:
        wait_for(lambda: recorder.snapshot()["status"]["state"] == "storage_error")
        assert recorder.store.sessions()["total"] == 0
        Usage.free = 1024**3
        wait_for(lambda: recorder.snapshot()["status"]["recording"])
    finally:
        recorder.stop()


def test_single_writer_lock(tmp_path):
    settings, recorder = setup(tmp_path)
    recorder.start()
    try:
        second = Recorder(settings)
        with pytest.raises(RuntimeError, match="one worker"):
            second.start()
    finally:
        recorder.stop()


def test_crash_recovery_preserves_sessions(tmp_path):
    settings, recorder = setup(tmp_path)
    session_id = recorder.store.start_session(settings)
    recorder.start()
    try:
        assert recorder.store.session(session_id)["status"] == "interrupted"
    finally:
        recorder.stop()


def test_rotation_and_stale_detection(tmp_path):
    settings, recorder = setup(tmp_path)
    settings.session_duration_s = 1
    recorder.start()
    try:
        wait_for(lambda: recorder.store.sessions()["total"] >= 2)
        assert recorder.store.sessions()["items"][1]["status"] == "complete"
        with recorder.mutex:
            recorder.last_success = time.monotonic() - 30
        assert recorder.snapshot()["status"]["stale"] is True
        assert recorder.snapshot()["status"]["recording"] is False
    finally:
        recorder.stop()


def test_stream_capacity_released_when_client_disconnects():
    import asyncio
    import threading
    from starlette.requests import ClientDisconnect
    from oclab.app import ManagedStream

    slots = threading.BoundedSemaphore(1)
    async def scenario():
        for _ in range(3):
            assert slots.acquire(blocking=False)
            response = ManagedStream(iter([b"data"]), release=slots.release)
            async def send(message):
                raise OSError("client closed connection")
            async def receive():
                return {"type": "http.disconnect"}
            with pytest.raises(ClientDisconnect):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    asyncio.run(scenario())
