import time

import pytest
from fastapi.testclient import TestClient

from oclab.app import create_app
from oclab.config import Settings, load_settings
from oclab.service import Recorder

from test_recording import wait_for
from test_weather import rows

TOKEN = "correct-horse-battery-staple"
LOCAL = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.9", 50000)
CONTROL = {"X-OCLab-Control": "1"}


def settings(tmp_path, **changes):
    s = Settings(mode="demo", data_dir=str(tmp_path), capture_interval_s=0.1, retry_interval_s=0.1,
                 minimum_free_disk_mb=0, record_on_start=False, **changes)
    s.validate()
    return s


def test_live_view_saves_nothing_until_logging_starts(tmp_path):
    s = settings(tmp_path)
    recorder = Recorder(s)
    recorder.start()
    try:
        wait_for(lambda: recorder.snapshot()["status"]["state"] == "monitoring")
        wait_for(lambda: recorder.weather.snapshot()["counters"]["wind"] > 20)
        status = recorder.snapshot()["status"]
        assert status["acquiring"] and not status["logging"] and not status["recording"]
        latest = recorder.snapshot()["latest"]["summary"]
        assert latest["capture_id"] is None and latest["session_id"] is None and latest["weather_status"] == "ok"
        assert recorder.store.sessions()["total"] == 0
        time.sleep(1.2)  # Past a weather flush
        assert rows(recorder.store, "wind_samples") == []

        started = time.time_ns()
        recorder.set_logging(True)
        wait_for(lambda: recorder.store.sessions()["total"] == 1 and recorder.snapshot()["status"]["recording"])
        wait_for(lambda: recorder.store.session(recorder.session_id)["capture_count"] >= 2)
        wait_for(lambda: len(rows(recorder.store, "wind_samples")) > 0)
        # The buffered weather before logging started is saved, so the first capture's window is complete.
        assert min(r["host_received_ns"] for r in rows(recorder.store, "wind_samples")) < started - 1_000_000_000
        session_id = recorder.session_id

        recorder.set_logging(False)
        wait_for(lambda: recorder.store.session(session_id)["status"] == "complete")
        count = recorder.store.session(session_id)["capture_count"]
        wind = len(rows(recorder.store, "wind_samples"))
        time.sleep(1.5)
        assert recorder.store.session(session_id)["capture_count"] == count
        assert len(rows(recorder.store, "wind_samples")) == wind
        assert recorder.snapshot()["status"]["state"] == "monitoring"
        assert recorder.store.sessions()["total"] == 1
    finally:
        recorder.stop()


def test_sse_replay_uses_sequence_numbers_for_unsaved_captures(tmp_path):
    recorder = Recorder(settings(tmp_path))
    recorder.start()
    try:
        wait_for(lambda: len(recorder.captures_since(0)) >= 3)
        seqs = [c["seq"] for c in recorder.captures_since(0)]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert recorder.captures_since(seqs[-1]) == [] or recorder.captures_since(seqs[-1])[0]["seq"] > seqs[-1]
    finally:
        recorder.stop()


def test_logging_control_access_rules(tmp_path):
    s = settings(tmp_path, control_token=TOKEN)
    recorder = Recorder(s)
    app = create_app(s, recorder)
    with TestClient(app, client=LOCAL) as local:
        wait_for(lambda: recorder.snapshot()["status"]["acquiring"])
        assert local.get("/api/health").status_code == 200  # Healthy while live, even when not logging
        assert local.get("/api/health").json()["logging"] is False
        assert local.get("/api/control").json()["allowed"] is True
        assert local.post("/api/logging", json={"enabled": True}).status_code == 403  # No control header
        assert local.post("/api/logging", json={"enabled": True}, headers={**CONTROL, "Origin": "https://evil.example"}).status_code == 403
        response = local.post("/api/logging", json={"enabled": True}, headers={**CONTROL, "Origin": "http://testserver"})
        assert response.status_code == 200 and response.json()["logging"] is True
        # Through the HTTPS proxy the request is loopback but carries proxy headers: token required.
        proxied = {**CONTROL, "X-Forwarded-For": "198.51.100.7"}
        assert local.post("/api/logging", json={"enabled": False}, headers=proxied).status_code == 403
        assert local.get("/api/control", headers={"X-Forwarded-For": "198.51.100.7"}).json()["allowed"] is False
    remote = TestClient(app, client=REMOTE)
    assert remote.post("/api/logging", json={"enabled": False}, headers=CONTROL).status_code == 403
    assert remote.post("/api/logging", json={"enabled": False}, headers={**CONTROL, "X-Control-Token": "wrong-token-of-length"}).status_code == 403
    assert remote.get("/api/control", headers={"X-Control-Token": TOKEN}).json()["allowed"] is True
    response = remote.post("/api/logging", json={"enabled": False}, headers={**CONTROL, "X-Control-Token": TOKEN})
    assert response.status_code == 200 and response.json()["logging"] is False
    assert remote.post("/api/logging", json={"enabled": "maybe"}, headers={**CONTROL, "X-Control-Token": TOKEN}).status_code == 422


def test_without_token_only_this_pc_can_switch(tmp_path):
    s = settings(tmp_path)
    app = create_app(s, Recorder(s))
    info = TestClient(app, client=REMOTE).get("/api/control").json()
    assert info["allowed"] is False and info["token_available"] is False and "acquisition PC" in info["reason"]
    assert TestClient(app, client=REMOTE).post("/api/logging", json={"enabled": True}, headers={**CONTROL, "X-Control-Token": TOKEN}).status_code == 403


def test_control_token_is_never_published(tmp_path):
    s = settings(tmp_path, control_token=TOKEN)
    recorder = Recorder(s)
    assert "control_token" not in TestClient(create_app(s, recorder)).get("/api/config").json()
    session_id = recorder.store.start_session(s)
    assert TOKEN not in str(recorder.store.session(session_id)["metadata"])


@pytest.mark.parametrize("text", ["record_on_start = 1", 'control_token = "short"', "control_token = 5"])
def test_bad_control_settings_fail_early(tmp_path, text):
    path = tmp_path / "c.toml"
    path.write_text(text)
    with pytest.raises((ValueError, TypeError)):
        load_settings(str(path))
