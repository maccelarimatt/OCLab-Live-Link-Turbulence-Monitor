"""Read-only access to recorded SUM waveforms.

Two kinds of source hold the same stored series:

- a published dataset folder (``datasets/<night>/waveforms/*.h5``) or one HDF5 file, and
- the recorder's SQLite archive (a data directory or ``recordings.sqlite3``), opened
  read-only so it is safe while the recorder is writing.

Bursts are read a chunk at a time, so a long session is never held in memory.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3

import numpy as np

from ..storage import Store

RAIL_LOW, RAIL_HIGH = 28, 227   # Driver wire-code rails (see the README)
RAIL_MARGIN = 0.98              # Filtered SUM at or above 98 % of the rail voltage counts as clipped
CHUNK_BURSTS = 256


@dataclass
class Session:
    """One recording session: timing for every burst, and SUM read on demand."""
    id: str
    started_utc: str
    ended_utc: str
    status: str
    kind: str                     # "hdf5" or "sqlite"
    path: Path
    metadata: dict
    rate_hz: int                  # Stored samples per second within a burst
    t0_s: float                   # Time of stored sample 0 after the burst's first scope sample
    samples_per_burst: int
    sum_channel: int              # 1-based scope channel
    sum_volts_per_count: float    # 0 for sources stored in volts (schema 1)
    capture_id: np.ndarray
    started_ns: np.ndarray
    received_ns: np.ndarray
    _scope: dict = field(default_factory=dict, repr=False)

    @property
    def bursts(self) -> int:
        return len(self.capture_id)

    @property
    def span_s(self) -> float:
        """Length of the stored series of one burst."""
        return self.samples_per_burst / self.rate_hz

    @property
    def calibration(self) -> dict:
        return self.metadata.get("settings", {}).get("calibration", {})

    @property
    def rail_v(self) -> float:
        """SUM voltage at the scope's upper ADC rail (nominal code 255, 127 codes above mid-scale)."""
        scope = self._scope or self.metadata.get("settings", {}).get("scope", {})
        index = self.sum_channel - 1
        volts_per_div = scope.get("volts_per_div", [1.0] * 4)[index]
        probe = scope.get("probe_factors", [1.0] * 4)[index]
        return 127 / 31 * volts_per_div * probe

    def thresholds(self) -> tuple[float, float]:
        """(low, high): SUM below low is too weak, above high saturated or at the rail."""
        low = float(self.calibration.get("minimum_sum_v") or 0.0)
        high = RAIL_MARGIN * self.rail_v
        if self.calibration.get("saturation_v"):
            high = min(high, float(self.calibration["saturation_v"]))
        return low, high

    def read(self, first: int, stop: int):
        """SUM volts (n, m), SUM rail flags (n, m) or None, any-channel clip flags (n, m) or None."""
        if self.kind == "hdf5":
            import h5py
            with h5py.File(self.path, "r") as f:
                codes = f["codes"][first:stop, self.sum_channel - 1]
                clipped = f["clipped"][first:stop]
            return codes * self.sum_volts_per_count, None, clipped
        ids = self.capture_id[first:stop]
        if not len(ids):
            empty = np.empty((0, self.samples_per_burst))
            return empty, None, None
        with _connect(self.path) as db:
            rows = db.execute("SELECT id, waveform FROM captures WHERE session_id=? AND id>=? AND id<=? ORDER BY id",
                              (self.id, int(ids[0]), int(ids[-1]))).fetchall()
        if [r[0] for r in rows] != list(ids):
            raise RuntimeError(f"{self.id}: captures changed while reading")
        volts, rails, clips = [], [], []
        index = self.sum_channel - 1
        for _, blob in rows:
            _, v, clipped, raw = Store.decode(blob, self._scope)
            volts.append(v[index])
            clips.append(clipped)
            rails.append(None if raw is None else (raw[index] <= RAIL_LOW) | (raw[index] >= RAIL_HIGH))
        return (np.array(volts), None if rails[0] is None else np.array(rails),
                None if clips[0] is None else np.array(clips))

    def chunks(self, first: int = 0, stop: int | None = None, size: int = CHUNK_BURSTS):
        """Yield (first burst index, SUM, rail, clipped) a chunk at a time."""
        stop = self.bursts if stop is None else min(stop, self.bursts)
        for start in range(first, stop, size):
            yield (start, *self.read(start, min(start + size, stop)))


def _connect(path: Path):
    return closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15))


def open_source(path) -> list[Session]:
    """All sessions in a dataset folder, an HDF5 file, a data directory or a SQLite archive."""
    path = Path(path)
    if path.is_dir():
        if (path / "recordings.sqlite3").exists():
            return _sqlite_sessions(path / "recordings.sqlite3")
        files = sorted((path / "waveforms").glob("*.h5")) or sorted(path.glob("*.h5"))
        if not files:
            raise ValueError(f"{path}: no recordings.sqlite3, waveforms/*.h5 or *.h5 files")
        return [_hdf5_session(p) for p in files]
    if path.suffix == ".h5":
        return [_hdf5_session(path)]
    if path.suffix in (".sqlite3", ".sqlite", ".db"):
        return _sqlite_sessions(path)
    raise ValueError(f"{path}: not a dataset folder, HDF5 file or SQLite archive")


def find_session(path, session_id: str | None) -> Session:
    sessions = open_source(path)
    if session_id is None:
        if len(sessions) == 1:
            return sessions[0]
        raise ValueError(f"{path} holds {len(sessions)} sessions; choose one with --session "
                         f"(run 'inspect' to list them)")
    matches = [s for s in sessions if s.id == session_id or s.id.startswith(session_id)]
    if len(matches) != 1:
        raise ValueError(f"{len(matches)} sessions match {session_id!r} in {path}")
    return matches[0]


def _hdf5_session(path: Path) -> Session:
    try:
        import h5py
    except ImportError:
        raise RuntimeError("Reading HDF5 datasets needs h5py: .venv/bin/python -m pip install -e '.[export]'") from None
    with h5py.File(path, "r") as f:
        a = f.attrs
        metadata = json.loads(a["metadata_json"])
        sum_channel = int(a["sum_channel"])
        return Session(
            id=str(a["session_id"]), started_utc=str(a["started_utc"]), ended_utc=str(a["ended_utc"]),
            status=str(a["status"]), kind="hdf5", path=path, metadata=metadata,
            rate_hz=int(a["store_rate_hz"]), t0_s=float(a["t0_s"]), samples_per_burst=int(f["codes"].shape[2]),
            sum_channel=sum_channel, sum_volts_per_count=float(a["volts_per_count"][sum_channel - 1]),
            capture_id=f["capture_id"][...], started_ns=f["host_started_ns"][...],
            received_ns=f["host_received_ns"][...], _scope=metadata["settings"]["scope"])


def _sqlite_sessions(path: Path) -> list[Session]:
    if not path.exists():
        raise ValueError(f"{path} does not exist")
    sessions = []
    with _connect(path) as db:
        rows = db.execute("SELECT id, started_utc, ended_utc, status, metadata FROM sessions ORDER BY started_utc").fetchall()
        for session_id, started, ended, status, metadata in rows:
            metadata = json.loads(metadata)
            ids, started_ns, received_ns, after = [], [], [], 0
            while True:  # Bounded batches; no long read transaction
                batch = db.execute("SELECT id, summary FROM captures WHERE session_id=? AND id>? ORDER BY id LIMIT 2000",
                                   (session_id, after)).fetchall()
                if not batch:
                    break
                after = batch[-1][0]
                for capture_id, summary in batch:
                    s = json.loads(summary)
                    ids.append(capture_id)
                    started_ns.append(s["host_started_ns"])
                    received_ns.append(s["host_received_ns"])
            if not ids:
                continue
            scope = metadata["settings"]["scope"]
            blob = db.execute("SELECT waveform FROM captures WHERE id=?", (ids[0],)).fetchone()[0]
            t, v, _, _ = Store.decode(blob, scope)
            rate = round(1 / float(np.median(np.diff(t)))) if len(t) > 1 else int(scope["sample_rate"])
            sessions.append(Session(
                id=session_id, started_utc=started, ended_utc=ended or "", status=status, kind="sqlite", path=path,
                metadata=metadata, rate_hz=int(rate), t0_s=float(t[0]), samples_per_burst=v.shape[1],
                sum_channel=int(scope["sum_channel"]), sum_volts_per_count=0.0,
                capture_id=np.array(ids, dtype=np.int64), started_ns=np.array(started_ns, dtype=np.int64),
                received_ns=np.array(received_ns, dtype=np.int64), _scope=scope))
    return sessions
