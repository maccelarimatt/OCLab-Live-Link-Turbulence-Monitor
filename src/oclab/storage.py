from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import uuid
import zlib

import numpy as np

from . import __version__

DRIVER_REVISION = "be5663df4f0a93e96c039071f9502c698da678a1"
SUMMARY_COLUMNS = [
    "capture_id", "session_id", "source", "host_started_utc", "host_received_utc",
    "host_started_ns", "host_received_ns", "acquisition_elapsed_s", "since_previous_start_s",
    "sample_count", "sample_rate_hz", "burst_duration_s",
    "x_mean_v", "y_mean_v", "sum_mean_v", "x_std_v", "y_std_v", "sum_std_v",
    "position_x", "position_y", "position_unit", "wander_rms",
    "valid_fraction", "clipped_fraction", "low_sum_fraction",
    # Schema 2: weather associated with each capture (see weather.associate).
    "wind_mean_ms", "wind_std_ms", "wind_max_ms", "wind_n", "wind_ti", "taylor_ok",
    "air_temp_c", "dht_temp_c", "rh_pct", "pressure_hpa", "light_v", "ldr_ohm",
    "weather_age_s", "weather_status",
]
WEATHER_COLUMNS = [
    "kind", "host_received_utc", "host_received_ns", "source", "pico_t_ms",
    "v_adc", "i_ma", "wind_ms", "clip",
    "dht_c", "dht_rh", "baro_hpa", "baro_c", "light_v", "light_x", "raw",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ns_to_utc(value: int) -> str:
    return datetime.fromtimestamp(value / 1e9, timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, data_dir: str):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "recordings.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, started_utc TEXT NOT NULL, ended_utc TEXT,
                    status TEXT NOT NULL, mode TEXT NOT NULL, metadata TEXT NOT NULL,
                    capture_count INTEGER NOT NULL DEFAULT 0,
                    sample_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS captures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    received_ns INTEGER NOT NULL, summary TEXT NOT NULL, waveform BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS capture_session ON captures(session_id, id);
                -- Weather is a continuous time series, independent of scope sessions.
                CREATE TABLE IF NOT EXISTS wind_samples (
                    id INTEGER PRIMARY KEY, host_received_ns INTEGER NOT NULL, source TEXT NOT NULL,
                    pico_t_ms INTEGER NOT NULL, v_adc REAL, i_ma REAL, wind_ms REAL, clip INTEGER,
                    raw TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS wind_time ON wind_samples(host_received_ns);
                CREATE TABLE IF NOT EXISTS env_samples (
                    id INTEGER PRIMARY KEY, host_received_ns INTEGER NOT NULL, source TEXT NOT NULL,
                    pico_t_ms INTEGER NOT NULL, dht_c REAL, dht_rh REAL, baro_hpa REAL, baro_c REAL,
                    light_v REAL, light_x REAL, raw TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS env_time ON env_samples(host_received_ns);
                CREATE TABLE IF NOT EXISTS weather_headers (
                    id INTEGER PRIMARY KEY, host_received_ns INTEGER NOT NULL, source TEXT NOT NULL,
                    header TEXT NOT NULL, raw TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            # The last actual write time is the best available end time after a crash.
            rows = db.execute("SELECT id FROM sessions WHERE status='recording'").fetchall()
            for row in rows:
                last = db.execute("SELECT received_ns FROM captures WHERE session_id=? ORDER BY id DESC LIMIT 1", (row[0],)).fetchone()
                db.execute("UPDATE sessions SET status='interrupted', ended_utc=? WHERE id=?",
                           (ns_to_utc(last[0]) if last else utc_now(), row[0]))

    def start_session(self, settings, weather=None) -> str:
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        metadata = {
            "schema_version": 2, "application_version": __version__,
            "driver_repository": "https://github.com/maccelarimatt/Hantek6254BC-Linux-Driver",
            "driver_revision": DRIVER_REVISION, "settings": settings.public(),
            "time_basis": "UTC host call-start and receive timestamps; sample time is relative within each burst. No hardware UTC timestamp. Bursts are not contiguous.",
            "statistics": "Voltage mean and population std use all samples. Position mean and radial RMS about the burst centroid use only unclipped samples with corrected SUM >= minimum_sum_v.",
            "raw_format": "Per-capture NPZ: scope_volts (4,N), raw (4,N) upstream wire codes, time_s (N). CSV ch*_v applies probe_factors to scope_volts. No offset correction applied to archived voltage columns.",
            "weather": weather,
        }
        with self.connect() as db:
            db.execute("INSERT INTO sessions(id,started_utc,status,mode,metadata) VALUES(?,?,?,?,?)",
                       (session_id, utc_now(), "recording", settings.mode, json.dumps(metadata)))
        return session_id

    def finish_session(self, session_id, status="complete"):
        if session_id:
            with self.connect() as db:
                db.execute("UPDATE sessions SET ended_utc=?,status=? WHERE id=?", (utc_now(), status, session_id))

    def save(self, session_id, capture, summary: dict) -> dict:
        buf = io.BytesIO()
        np.savez_compressed(buf, scope_volts=capture.volts, raw=capture.raw, time_s=capture.time_s)
        result = {**summary, "session_id": session_id}
        with self.connect() as db:
            cursor = db.execute("INSERT INTO captures(session_id,received_ns,summary,waveform) VALUES(?,?,?,?)",
                                (session_id, result["host_received_ns"], json.dumps(result, allow_nan=False), buf.getvalue()))
            result["capture_id"] = cursor.lastrowid
            db.execute("UPDATE sessions SET capture_count=capture_count+1,sample_count=sample_count+? WHERE id=?",
                       (result["sample_count"], session_id))
        return result

    def save_weather(self, source, wind, env, headers=()):
        # One transaction per batch (about once per second) keeps 10 Hz wind
        # inserts from contending with capture writes.
        with self.connect() as db:
            db.executemany("INSERT INTO wind_samples(host_received_ns,source,pico_t_ms,v_adc,i_ma,wind_ms,clip,raw) VALUES(?,?,?,?,?,?,?,?)",
                           [(w["host_received_ns"], source, w["t_ms"], w["v_adc"], w["i_ma"], w["wind_ms"], w["clip"], w["raw"]) for w in wind])
            db.executemany("INSERT INTO env_samples(host_received_ns,source,pico_t_ms,dht_c,dht_rh,baro_hpa,baro_c,light_v,light_x,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           [(e["host_received_ns"], source, e["t_ms"], e["dht_c"], e["dht_rh"], e["baro_hpa"], e["baro_c"], e["light_v"], e["light_x"], e["raw"]) for e in env])
            db.executemany("INSERT INTO weather_headers(host_received_ns,source,header,raw) VALUES(?,?,?,?)",
                           [(h["host_received_ns"], source, json.dumps(h["header"]), h["raw"]) for h in headers])

    def weather_history(self, source, since_ns):
        """Wind as 1 s bins (mean/max/count of non-null values) and every env sample since since_ns."""
        with self.connect() as db:
            db.execute("BEGIN")
            wind = db.execute(
                "SELECT host_received_ns/1000000000 AS second, avg(wind_ms) AS mean_ms, max(wind_ms) AS max_ms, count(wind_ms) AS n "
                "FROM wind_samples WHERE source=? AND host_received_ns>? GROUP BY second ORDER BY second", (source, since_ns)).fetchall()
            env = db.execute(
                "SELECT host_received_ns,pico_t_ms,dht_c,dht_rh,baro_hpa,baro_c,light_v,light_x "
                "FROM env_samples WHERE source=? AND host_received_ns>? ORDER BY host_received_ns", (source, since_ns)).fetchall()
        return {"wind": [{"t_ns": r["second"] * 1_000_000_000 + 500_000_000, "mean_ms": r["mean_ms"], "max_ms": r["max_ms"], "n": r["n"]}
                         for r in wind if r["n"]],
                "env": [dict(r) for r in env]}

    def weather_csv(self, source, from_ns, through_ns):
        """Every W and E sample with from_ns <= host time <= through_ns, merged in time order."""
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(WEATHER_COLUMNS)
        yield stream.getvalue().encode()
        query = """
            SELECT * FROM (
                SELECT 'W' AS kind, id, host_received_ns, source, pico_t_ms, v_adc, i_ma, wind_ms, clip,
                       NULL AS dht_c, NULL AS dht_rh, NULL AS baro_hpa, NULL AS baro_c, NULL AS light_v, NULL AS light_x, raw
                FROM wind_samples WHERE source=? AND host_received_ns>=? AND host_received_ns<=?
                UNION ALL
                SELECT 'E', id, host_received_ns, source, pico_t_ms, NULL, NULL, NULL, NULL,
                       dht_c, dht_rh, baro_hpa, baro_c, light_v, light_x, raw
                FROM env_samples WHERE source=? AND host_received_ns>=? AND host_received_ns<=?
            ) WHERE (host_received_ns, kind, id) > (?, ?, ?) ORDER BY host_received_ns, kind, id LIMIT 2000"""
        last = (from_ns, "", 0)  # Every kind sorts after "", so from_ns itself is included.
        while True:
            # Bounded batches; no read transaction is held between them.
            with self.connect() as db:
                rows = db.execute(query, (source, last[0], through_ns, source, last[0], through_ns, *last)).fetchall()
            if not rows:
                break
            stream.seek(0)
            stream.truncate(0)
            for row in rows:
                data = {**dict(row), "host_received_utc": ns_to_utc(row["host_received_ns"])}
                writer.writerow([data[key] for key in WEATHER_COLUMNS])
            last = (rows[-1]["host_received_ns"], rows[-1]["kind"], rows[-1]["id"])
            yield stream.getvalue().encode()

    def sessions(self, limit=20, offset=0):
        with self.connect() as db:
            db.execute("BEGIN")
            total = db.execute("SELECT count(*) FROM sessions").fetchone()[0]
            rows = db.execute("SELECT id,started_utc,ended_utc,status,mode,capture_count,sample_count FROM sessions ORDER BY rowid DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    def session(self, session_id):
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["metadata"] = json.loads(result["metadata"])
            result["export_through_capture_id"] = db.execute("SELECT COALESCE(MAX(id),0) FROM captures WHERE session_id=?", (session_id,)).fetchone()[0]
            return result

    def history(self, limit=600):
        with self.connect() as db:
            rows = db.execute("SELECT id,summary FROM captures ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**json.loads(r["summary"]), "capture_id": r["id"]} for r in reversed(rows)]

    def export_rows(self, session_id, through_id, raw=False):
        # Bounded batches release the read transaction between batches so slow
        # downloads cannot pin the WAL indefinitely. Captures are immutable.
        after = 0
        while True:
            with self.connect() as db:
                rows = db.execute(
                    f"SELECT id,summary{',waveform' if raw else ''} FROM captures WHERE session_id=? AND id>? AND id<=? ORDER BY id LIMIT 16",
                    (session_id, after, through_id)).fetchall()
            if not rows:
                break
            for row in rows:
                after = row["id"]
                yield row

    def summary_csv(self, session):
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(SUMMARY_COLUMNS)
        yield stream.getvalue().encode()
        for row in self.export_rows(session["id"], session["export_through_capture_id"]):
            data = {**json.loads(row["summary"]), "capture_id": row["id"]}
            stream.seek(0)
            stream.truncate(0)
            writer.writerow([data.get(key) for key in SUMMARY_COLUMNS])
            yield stream.getvalue().encode()

    def raw_csv_gzip(self, session):
        compressor = zlib.compressobj(level=3, wbits=31)
        header = ["capture_id", "source", "host_started_utc", "host_received_utc", "sample_index", "sample_time_s"]
        header += [f"ch{i}_v" for i in range(1, 5)] + [f"ch{i}_raw" for i in range(1, 5)]
        yield compressor.compress((",".join(header) + "\r\n").encode())
        factors = np.array(session["metadata"]["settings"]["scope"]["probe_factors"])[:, None]
        for row in self.export_rows(session["id"], session["export_through_capture_id"], raw=True):
            summary = json.loads(row["summary"])
            with np.load(io.BytesIO(row["waveform"]), allow_pickle=False) as waveform:
                v, raw, t = waveform["scope_volts"] * factors, waveform["raw"], waveform["time_s"]
            out = io.StringIO()
            writer = csv.writer(out)
            for i in range(len(t)):
                writer.writerow([row["id"], summary["source"], summary["host_started_utc"], summary["host_received_utc"], i, t[i], *v[:, i], *raw[:, i]])
            chunk = compressor.compress(out.getvalue().encode())
            if chunk:
                yield chunk
        yield compressor.flush()
