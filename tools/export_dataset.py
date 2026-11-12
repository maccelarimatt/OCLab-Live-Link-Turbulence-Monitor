#!/usr/bin/env python3
"""Export recorded sessions as a compact, self-describing dataset folder.

    .venv/bin/python -m pip install h5py
    .venv/bin/python tools/export_dataset.py --data-dir data --out datasets/2026-11-11_night \\
        --from 2026-11-11T17:00 --to 2026-11-12T07:00        # session start times, UTC

Reads the archive read-only (safe while the recorder runs) and writes:

    waveforms/<session id>.h5   stored samples of every capture, lossless (HDF5, shuffle + gzip)
    captures.csv.gz             one row per capture: summary statistics and associated weather
    weather.csv.gz              every wind (10 Hz) and environment (1 Hz) sample in the time span
    sessions.json               session records with full settings/calibration metadata
    checksums.sha256            SHA-256 of every file above

Every waveform file is read back and compared with the archive before the export is reported.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oclab.decimation import volts_per_count  # noqa: E402
from oclab.storage import SUMMARY_COLUMNS, WEATHER_COLUMNS, ns_to_utc, utc_to_ns  # noqa: E402


def write_gzip_csv(path: Path, header, rows) -> int:
    count = 0
    with gzip.GzipFile(path, "wb", compresslevel=9, mtime=0) as raw, io.TextIOWrapper(raw, newline="") as text:
        writer = csv.writer(text)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def export(data_dir: Path, out: Path, start_utc: str, end_utc: str, mode: str) -> dict:
    import h5py

    db = sqlite3.connect(f"file:{data_dir / 'recordings.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    sessions = [dict(r) for r in db.execute(
        "SELECT * FROM sessions WHERE mode=? AND started_utc>=? AND started_utc<? ORDER BY rowid", (mode, start_utc, end_utc))]
    sessions = [s for s in sessions if s["capture_count"] > 0]
    if not sessions:
        raise SystemExit("No sessions with captures in that range")
    (out / "waveforms").mkdir(parents=True, exist_ok=True)
    totals = {"sessions": len(sessions), "captures": 0, "stored_samples_per_channel": 0}
    summaries = []
    for session in sessions:
        session["metadata"] = json.loads(session["metadata"])
        if session["metadata"].get("schema_version", 1) < 3:
            raise SystemExit(f"{session['id']} predates the decimated format (schema 3)")
        scope = session["metadata"]["settings"]["scope"]
        ids, started, received, codes, clipped, t0, rate = [], [], [], [], [], None, None
        for row in db.execute("SELECT id, summary, waveform FROM captures WHERE session_id=? ORDER BY id", (session["id"],)):
            summary = {**json.loads(row["summary"]), "capture_id": row["id"]}
            summaries.append(summary)
            with np.load(io.BytesIO(row["waveform"]), allow_pickle=False) as w:
                codes.append(w["codes"]); clipped.append(w["clipped"])
                t0, rate = float(w["t0_s"]), int(w["store_rate_hz"])
            ids.append(row["id"]); started.append(summary["host_started_ns"]); received.append(summary["host_received_ns"])
        codes, clipped = np.array(codes, dtype=np.int16), np.array(clipped, dtype=bool)
        path = out / "waveforms" / f"{session['id']}.h5"
        with h5py.File(path, "w") as f:
            f.attrs.update(
                session_id=session["id"], mode=session["mode"], status=session["status"],
                started_utc=session["started_utc"], ended_utc=session["ended_utc"] or "",
                store_rate_hz=rate, t0_s=t0, scope_sample_rate_hz=scope["sample_rate"],
                volts_per_count=volts_per_count(scope["volts_per_div"], scope["probe_factors"]),
                channels=["CH1", "CH2", "CH3", "CH4"],
                x_channel=scope["x_channel"], y_channel=scope["y_channel"], sum_channel=scope["sum_channel"],
                description="codes[capture, channel, k] are int16 scope ADC codes x 64 about mid-scale, after anti-alias "
                            "filtering and decimation to store_rate_hz. volts = codes * volts_per_count[channel] "
                            "(probe-corrected, no offset correction). Sample k of a capture is at t0_s + k / store_rate_hz "
                            "after that burst's first scope sample; bursts are not contiguous. clipped[capture, k] is true "
                            "where a mapped X/Y/SUM sample under the filter reached the scope's ADC rails.",
                metadata_json=json.dumps(session["metadata"]))
            f.create_dataset("codes", data=codes, chunks=(min(64, len(codes)), 4, codes.shape[2]), shuffle=True,
                             compression="gzip", compression_opts=9)
            f.create_dataset("clipped", data=clipped, chunks=True, compression="gzip", compression_opts=9)
            f.create_dataset("capture_id", data=np.array(ids, dtype=np.int64))
            f.create_dataset("host_started_ns", data=np.array(started, dtype=np.int64))
            f.create_dataset("host_received_ns", data=np.array(received, dtype=np.int64))
        with h5py.File(path, "r") as f:  # Verify the file against the archive values just read
            if not (np.array_equal(f["codes"][...], codes) and np.array_equal(f["clipped"][...], clipped)
                    and np.array_equal(f["capture_id"][...], ids)):
                raise SystemExit(f"Verification failed for {path}")
        totals["captures"] += len(ids)
        totals["stored_samples_per_channel"] += int(codes.shape[0] * codes.shape[2])
        print(f"  {session['id']}  {len(ids):5d} captures  {path.stat().st_size / 1e6:6.1f} MB  verified", flush=True)

    write_gzip_csv(out / "captures.csv.gz", SUMMARY_COLUMNS, ([s.get(k) for k in SUMMARY_COLUMNS] for s in summaries))
    # Weather: from one association window before the first session to the end of the last.
    window_s = (sessions[0]["metadata"].get("weather") or {}).get("association", {}).get("window_s", 10)
    from_ns = utc_to_ns(sessions[0]["started_utc"]) - round(window_s * 1e9)
    through_ns = utc_to_ns(sessions[-1]["ended_utc"]) if sessions[-1]["ended_utc"] else max(s["host_received_ns"] for s in summaries)
    query = """
        SELECT 'W' AS kind, host_received_ns, source, pico_t_ms, v_adc, i_ma, wind_ms, clip,
               NULL AS dht_c, NULL AS dht_rh, NULL AS baro_hpa, NULL AS baro_c, NULL AS light_v, NULL AS light_x, NULL AS baro_rh, raw
        FROM wind_samples WHERE source=:mode AND host_received_ns BETWEEN :a AND :b
        UNION ALL
        SELECT 'E', host_received_ns, source, pico_t_ms, NULL, NULL, NULL, NULL,
               dht_c, dht_rh, baro_hpa, baro_c, light_v, light_x, baro_rh, raw
        FROM env_samples WHERE source=:mode AND host_received_ns BETWEEN :a AND :b
        ORDER BY host_received_ns, kind"""
    rows = ({**dict(r), "host_received_utc": ns_to_utc(r["host_received_ns"])} for r in db.execute(query, {"mode": mode, "a": from_ns, "b": through_ns}))
    totals["weather_rows"] = write_gzip_csv(out / "weather.csv.gz", WEATHER_COLUMNS, ([r[k] for k in WEATHER_COLUMNS] for r in rows))
    headers = [json.loads(r[0]) for r in db.execute(
        "SELECT header FROM weather_headers WHERE source=? AND host_received_ns BETWEEN ? AND ? ORDER BY id DESC LIMIT 1", (mode, from_ns, through_ns))]
    (out / "sessions.json").write_text(json.dumps({
        "exported_from_utc": ns_to_utc(from_ns), "exported_through_utc": ns_to_utc(through_ns), "totals": totals,
        "weather_station_header": headers[0] if headers else None, "sessions": sessions}, indent=1) + "\n")
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "checksums.sha256" and p.suffix in (".h5", ".gz", ".json"))
    (out / "checksums.sha256").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out).as_posix()}\n" for p in files))
    totals["bytes"] = sum(p.stat().st_size for p in files)
    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--from", dest="start", required=True, help="first session start, UTC ISO, e.g. 2026-11-11T17:00")
    parser.add_argument("--to", dest="end", required=True, help="exclusive upper bound on session start, UTC ISO")
    parser.add_argument("--mode", default="hardware", choices=("hardware", "demo"))
    args = parser.parse_args()
    result = export(args.data_dir, args.out, args.start, args.end, args.mode)
    print(f"Exported {result['sessions']} sessions, {result['captures']:,} captures, {result['weather_rows']:,} weather rows: "
          f"{result['bytes'] / 1e6:.0f} MB in {args.out}")
