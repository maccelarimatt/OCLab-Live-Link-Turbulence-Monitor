import csv
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")

from oclab.config import Settings
from oclab.service import Recorder
from oclab.storage import Store

from test_recording import wait_for


def load_tool():
    spec = importlib.util.spec_from_file_location("export_dataset", Path(__file__).parents[1] / "tools/export_dataset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_is_lossless_and_complete(tmp_path):
    settings = Settings(mode="demo", data_dir=str(tmp_path / "data"), capture_interval_s=0, retry_interval_s=0.1,
                        minimum_free_disk_mb=0, session_duration_s=2)
    recorder = Recorder(settings)
    recorder.start()
    try:
        wait_for(lambda: recorder.store.sessions()["total"] >= 2, timeout=15)
    finally:
        recorder.stop()
    out = tmp_path / "dataset"
    totals = load_tool().export(tmp_path / "data", out, "2000-01-01", "2100-01-01", "demo")
    store = Store(settings.data_dir)
    sessions = store.sessions(limit=100)["items"]
    assert totals["sessions"] == len(sessions) and totals["captures"] == sum(s["capture_count"] for s in sessions)

    # Every stored sample, identical to the app's own decoder
    for item in sessions:
        session = store.session(item["id"])
        with h5py.File(out / "waveforms" / f"{item['id']}.h5") as f:
            assert f["codes"].dtype == np.int16 and f["codes"].shape[0] == item["capture_count"]
            volts = f["codes"][...] * f.attrs["volts_per_count"][:, None]
            for k, row in enumerate(store.export_rows(item["id"], session["export_through_capture_id"], raw=True)):
                _, expected, clipped, _ = Store.decode(row["waveform"], session["metadata"]["settings"]["scope"])
                assert f["capture_id"][k] == row["id"]
                assert np.array_equal(volts[k], expected) and np.array_equal(f["clipped"][k], clipped)
            assert json.loads(f.attrs["metadata_json"])["schema_version"] == 3

    rows = list(csv.DictReader(io.TextIOWrapper(gzip.open(out / "captures.csv.gz"), newline="")))
    assert len(rows) == totals["captures"] and rows[0]["source"] == "demo"
    weather = list(csv.DictReader(io.TextIOWrapper(gzip.open(out / "weather.csv.gz"), newline="")))
    assert len(weather) == totals["weather_rows"] > 0 and {r["kind"] for r in weather} == {"W", "E"}
    times = [int(r["host_received_ns"]) for r in weather]
    assert times == sorted(times)
    for line in (out / "checksums.sha256").read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert "control_token" not in (out / "sessions.json").read_text()


def test_export_with_no_sessions_in_range_stops(tmp_path):
    Store(str(tmp_path / "data"))
    with pytest.raises(SystemExit, match="No sessions"):
        load_tool().export(tmp_path / "data", tmp_path / "out", "2000-01-01", "2000-01-02", "hardware")
