import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from oclab.app import create_app
from oclab.config import Settings
from oclab.service import Recorder
from oclab.storage import SessionActive

from test_logging_control import CONTROL, LOCAL, REMOTE, TOKEN
from test_weather import rows

S = 1_000_000_000


def add_session(store, settings, n, status="complete", start_ns=1_800_000_000 * S, **values):
    """Insert a finished session with n captures whose summaries hold the given per-capture values."""
    session_id = store.start_session(settings)
    with store.connect() as db:
        for i in range(n):
            summary = {"host_received_ns": start_ns + i * S // 2, "session_id": session_id, "source": settings.mode,
                       "sample_count": 4096, **{k: (v(i) if callable(v) else v) for k, v in values.items()}}
            db.execute("INSERT INTO captures(session_id,received_ns,summary,waveform) VALUES(?,?,?,?)",
                       (session_id, summary["host_received_ns"], json.dumps(summary), b"x"))
        db.execute("UPDATE sessions SET status=?, capture_count=? WHERE id=?", (status, n, session_id))
    return session_id


@pytest.fixture
def app_and_store(tmp_path):
    settings = Settings(mode="demo", data_dir=str(tmp_path), control_token=TOKEN)
    recorder = Recorder(settings)
    return create_app(settings, recorder), recorder.store, settings


def test_delete_session_removes_only_that_session(app_and_store):
    app, store, settings = app_and_store
    keep = add_session(store, settings, 5, x_mean_v=1.0)
    gone = add_session(store, settings, 7, x_mean_v=2.0)
    store.save_weather("demo", [{"host_received_ns": 1_800_000_000 * S, "t_ms": 1, "v_adc": 0.8, "i_ma": 5.0,
                                 "wind_ms": 2.0, "clip": 0, "raw": "W,1"}], [])
    local = TestClient(app, client=LOCAL)
    response = local.delete(f"/api/sessions/{gone}", headers=CONTROL)
    assert response.status_code == 200 and response.json() == {"deleted": gone, "captures": 7}
    assert store.session(gone) is None
    assert store.session(keep)["capture_count"] == 5
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM captures").fetchone()[0] == 5
    assert len(rows(store, "wind_samples")) == 1  # Weather is a shared time series and is kept
    assert local.delete(f"/api/sessions/{gone}", headers=CONTROL).status_code == 404


def test_delete_requires_control_and_refuses_recording_session(app_and_store):
    app, store, settings = app_and_store
    session = add_session(store, settings, 3)
    live = add_session(store, settings, 3, status="recording")
    remote = TestClient(app, client=REMOTE)
    assert remote.delete(f"/api/sessions/{session}", headers=CONTROL).status_code == 403  # No token
    assert remote.delete(f"/api/sessions/{session}", headers={"X-Control-Token": TOKEN}).status_code == 403  # No header
    assert TestClient(app, client=LOCAL).delete(f"/api/sessions/{session}", headers={**CONTROL, "Origin": "https://evil.example"}).status_code == 403
    assert store.session(session) is not None
    assert remote.delete(f"/api/sessions/{live}", headers={**CONTROL, "X-Control-Token": TOKEN}).status_code == 409
    with pytest.raises(SessionActive):
        store.delete_session(session, active_id=session)
    assert remote.delete(f"/api/sessions/{session}", headers={**CONTROL, "X-Control-Token": TOKEN}).status_code == 200


def test_series_bins_long_sessions_and_keeps_nulls(app_and_store):
    app, store, settings = app_and_store
    session = add_session(store, settings, 1000, x_mean_v=lambda i: float(i), wind_mean_ms=lambda i: None if i < 500 else 3.0,
                          taylor_ok=True)
    client = TestClient(app)
    full = client.get(f"/api/sessions/{session}/series?fields=x_mean_v,wind_mean_ms&points=5000").json()
    assert full["captures"] == 1000 and not full["binned"] and len(full["t_ns"]) == 1000
    assert full["fields"]["x_mean_v"]["mean"][:3] == [0.0, 1.0, 2.0]
    assert full["fields"]["wind_mean_ms"]["mean"][0] is None  # Missing stays missing, never 0
    binned = client.get(f"/api/sessions/{session}/series?fields=x_mean_v&points=100").json()
    assert binned["binned"] and len(binned["t_ns"]) == 100
    x = binned["fields"]["x_mean_v"]
    assert x["min"][0] == 0 and x["max"][0] == 9 and x["mean"][0] == pytest.approx(4.5)  # 10 captures per bin
    assert np.all(np.diff(binned["t_ns"]) > 0)
    assert binned["units"] == {"x_mean_v": "V"}


def test_series_validates_fields(app_and_store):
    app, store, settings = app_and_store
    session = add_session(store, settings, 3, position_x=0.1)
    client = TestClient(app)
    assert client.get(f"/api/sessions/{session}/series?fields=raw_secrets").status_code == 422
    assert client.get(f"/api/sessions/{session}/series?fields=x_mean_v,x_mean_v").status_code == 422  # Duplicates
    assert client.get(f"/api/sessions/{session}/series?fields=" + ",".join(client.get("/api/plot-fields").json())).status_code == 200
    assert client.get("/api/sessions/nope/series?fields=x_mean_v").status_code == 404
    assert client.get(f"/api/sessions/{session}/series?fields=position_x").json()["units"]["position_x"] == "ratio"
    assert "scintillation_index" in client.get("/api/plot-fields").json()


def test_series_bins_never_straddle_a_long_gap(app_and_store):
    app, store, settings = app_and_store
    # 600 captures, then the PC sleeps for 12 h, then 100 more
    session = add_session(store, settings, 700, x_mean_v=1.0)
    with store.connect() as db:
        for row in db.execute("SELECT id, summary FROM captures WHERE session_id=? ORDER BY id LIMIT -1 OFFSET 600", (session,)).fetchall():
            data = json.loads(row["summary"]); data["host_received_ns"] += 12 * 3600 * S
            db.execute("UPDATE captures SET summary=? WHERE id=?", (json.dumps(data), row["id"]))
    result = TestClient(app).get(f"/api/sessions/{session}/series?fields=x_mean_v&points=70").json()
    t = np.array(result["t_ns"])
    sleep_start = 1_800_000_000 * S + 599 * S // 2
    assert not np.any((t > sleep_start) & (t < sleep_start + 12 * 3600 * S))  # No averaged point inside the gap
    assert result["captures"] == 700 and len(t) <= 72
