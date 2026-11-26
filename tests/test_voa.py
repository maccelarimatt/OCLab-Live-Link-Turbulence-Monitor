"""VOA playback without hardware: gaps, anti-aliasing, normalisation, calibration, safe codes, serial parsing."""
import io
import json
import sqlite3

import numpy as np
import pytest

from oclab.storage import Store
from oclab.voa import analysis, board, cli, drive, sources, trace

RATE = 2500


# --------------------------------------------------------------------------
# Gaps
# --------------------------------------------------------------------------

def starts_ns(intervals_s):
    return 1_700_000_000_000_000_000 + np.rint(np.concatenate([[0], np.cumsum(intervals_s)]) * 1e9).astype(np.int64)


def test_burst_mode_gaps_are_found_and_measured():
    span = 1602 / RATE
    started = starts_ns(np.full(9, 0.711))
    gaps = trace.burst_gaps(started, span)
    assert gaps == pytest.approx(np.full(9, 0.711 - span), abs=1e-9)
    assert trace.gap_mask(gaps, RATE).all()
    first, bursts, seconds = trace.longest_gap_free(started, span, RATE)
    assert bursts == 1 and seconds == pytest.approx(span)
    summary = trace.gap_summary(started, span, RATE)
    assert summary["gaps"] == 9 and summary["gap_median_ms"] == pytest.approx(70.2, abs=0.1)
    assert summary["missing_s"] == pytest.approx(9 * (0.711 - span))


def test_contiguous_bursts_have_no_gaps_and_the_longest_stretch_spans_them():
    span = 1.0
    intervals = np.array([1.0, 1.0005, 0.999, 1.0, 1.3, 1.0, 1.0])   # Timestamp jitter, then one real gap
    started = starts_ns(intervals)
    mask = trace.gap_mask(trace.burst_gaps(started, span), 1000)
    assert mask.tolist() == [False, False, False, False, True, False, False]
    first, bursts, seconds = trace.longest_gap_free(started, span, 1000)
    assert (first, bursts) == (0, 5) and seconds == pytest.approx(5.0, abs=2e-3)


# --------------------------------------------------------------------------
# Anti-alias filtering and resampling
# --------------------------------------------------------------------------

def rms(x):
    return np.sqrt(np.mean(np.square(x)))


@pytest.mark.parametrize("f0,expected", [(10, 1), (100, 1), (390, 1), (510, 0), (600, 0), (1100, 0)])
def test_resampler_passes_the_band_and_rejects_what_would_alias(f0, expected):
    r = trace.Resampler(RATE)
    assert (r.L, r.M) == (2, 5)
    t = np.arange(5 * RATE) / RATE
    y = r.process(np.sin(2 * np.pi * f0 * t))
    assert rms(y[100:-100]) * np.sqrt(2) == pytest.approx(expected, abs=1.5e-3)   # >= 57 dB down when 0


def test_resampler_output_is_at_1_khz_with_known_delay_and_unity_dc_gain():
    r = trace.Resampler(RATE)
    x = 2.5 + np.sin(2 * np.pi * 50 * np.arange(10 * RATE) / RATE)
    y = r.process(x)
    assert abs(y.size - 10_000) <= 40 and y.mean() == pytest.approx(2.5, abs=1e-3)
    # Output k is at delay + k ms on the input time axis.
    k = np.arange(y.size)
    expected = 2.5 + np.sin(2 * np.pi * 50 * (r.delay_s + k / 1000))
    assert np.max(np.abs(y - expected)) < 1e-3


def test_streaming_in_any_chunks_matches_one_pass():
    rng = np.random.default_rng(3)
    x = rng.normal(size=20_000)
    whole = trace.Resampler(RATE).process(x)
    r = trace.Resampler(RATE)
    cuts = np.sort(rng.choice(np.arange(1, x.size), 40, replace=False))
    pieces = [r.process(part) for part in np.split(x, cuts)]
    assert np.allclose(np.concatenate(pieces), whole)


def test_resampler_rates():
    same = trace.Resampler(1000)
    x = np.arange(10.0)
    assert np.array_equal(same.process(x), x) and same.describe()["filter"].startswith("none")
    slow = trace.Resampler(500)
    assert slow.upsampled and slow.describe()["interpolated_from_slower_source"]
    with pytest.raises(ValueError):
        trace.Resampler(2500.5)


def test_filter_design_matches_the_recorder():
    from oclab.decimation import fir
    assert np.allclose(trace.Resampler(RATE).h / 2, fir(5))   # Same Kaiser design at the intermediate rate


# --------------------------------------------------------------------------
# Sessions (fake) and preparation
# --------------------------------------------------------------------------

class FakeSession(sources.Session):
    """A session whose SUM comes from an array (bursts x samples)."""

    def __init__(self, volts, intervals_s, saturation_v=4.0, minimum_sum_v=0.5):
        n, m = volts.shape
        super().__init__(
            id="20260101T000000Z-fake", started_utc="2026-01-01T00:00:00+00:00", ended_utc="", status="complete",
            kind="hdf5", path="fake.h5",
            metadata={"schema_version": 3, "settings": {"scope": {"volts_per_div": [1.0] * 4, "probe_factors": [1.0] * 4},
                                                        "calibration": {"saturation_v": saturation_v, "minimum_sum_v": minimum_sum_v}}},
            rate_hz=RATE, t0_s=0.0, samples_per_burst=m, sum_channel=3, sum_volts_per_count=1.0,
            capture_id=np.arange(1, n + 1), started_ns=starts_ns(np.full(n - 1, intervals_s)),
            received_ns=starts_ns(np.full(n - 1, intervals_s)) + int(0.7e9))
        self.volts = volts

    def read(self, first, stop):
        return self.volts[first:stop], None, np.zeros(self.volts[first:stop].shape, bool)


def fading(n, m, seed=1, mean=2.0):
    rng = np.random.default_rng(seed)
    return mean * np.exp(0.2 * rng.normal(size=(n, m)) - 0.02)


def read_trace(path):
    return np.loadtxt(path, delimiter=",", skiprows=1)


def test_prepare_refuses_gapped_windows_unless_bursts_are_joined(tmp_path):
    session = FakeSession(fading(20, 1602), 0.711)
    with pytest.raises(ValueError, match="burst-mode.*longest gap-free stretch.*0.641 s.*--join-bursts"):
        trace.prepare(session, tmp_path / "t.csv", duration_s=5)
    p = trace.prepare(session, tmp_path / "t.csv", duration_s=5, join_bursts=True)
    assert p["continuity"]["bursts_joined"] and "not continuous" in p["continuity"]["warning"]
    assert p["continuity"]["gaps"] == p["window"]["bursts"] - 1
    assert p["window"]["samples"] == 5000 and read_trace(tmp_path / "t.csv").shape == (5000, 4)
    assert p["window"]["wall_clock_span_s"] > p["window"]["duration_s"]


def test_continuous_source_needs_no_join_and_is_normalised(tmp_path):
    session = FakeSession(fading(8, 2500, seed=2), 1.0)          # 1 s bursts every 1 s: contiguous
    dark = 0.1
    p = trace.prepare(session, tmp_path / "t.csv", dark_v=dark)
    data = read_trace(tmp_path / "t.csv")
    t, h, sum_v = data[:, 0], data[:, 1], data[:, 2]
    assert not p["continuity"]["bursts_joined"] and p["continuity"]["warning"] is None
    assert h.mean() == pytest.approx(1.0, abs=1e-5)
    assert np.allclose(h, (sum_v - dark) / (sum_v - dark).mean(), atol=2e-5)
    assert np.allclose(np.diff(t), 1e-3)
    assert p["statistics"]["scintillation_index"] == pytest.approx(h.var(), rel=1e-3)
    assert p["dark"] == {"value_v": dark, "source": "given with --dark"}
    saved = json.loads((tmp_path / "t.json").read_text())
    assert saved["resampling"]["taps"] == 183 and saved["trace_sha256"] == p["trace_sha256"]
    import hashlib
    assert hashlib.sha256((tmp_path / "t.csv").read_bytes()).hexdigest() == p["trace_sha256"]


def test_saturated_and_weak_samples_are_flagged(tmp_path):
    volts = np.full((4, 2500), 2.0)
    volts[1, 1000:1100] = 4.09       # Detector saturated / at the rail
    volts[2, 500:520] = 0.1          # Below the SUM threshold
    session = FakeSession(volts, 1.0)
    p = trace.prepare(session, tmp_path / "t.csv")
    flagged = read_trace(tmp_path / "t.csv")[:, 3].astype(bool)
    assert p["flags"]["input_saturated_fraction"] == pytest.approx(100 / volts.size)
    assert p["flags"]["input_below_threshold_fraction"] == pytest.approx(20 / volts.size)
    assert 0 < flagged.mean() < 0.15
    on = np.flatnonzero(flagged)
    # Flags sit where the bad input is (burst 1 at 1.40-1.44 s, burst 2 at 2.20-2.21 s, minus the filter delay).
    delay = trace.Resampler(RATE).delay_s
    times = on / 1000 + delay
    assert np.all(((times > 1.36) & (times < 1.48)) | ((times > 2.16) & (times < 2.25)))
    assert p["dark"]["source"].startswith("assumed 0 V")


def test_window_start_by_offset_and_utc(tmp_path):
    session = FakeSession(fading(30, 1602), 0.711)
    p = trace.prepare(session, tmp_path / "a.csv", start=5.0, duration_s=1, join_bursts=True)
    assert p["window"]["first_capture_id"] == 9                           # First burst starting at or after 5 s
    utc = p["window"]["first_burst_started_utc"]
    q = trace.prepare(session, tmp_path / "b.csv", start=utc, duration_s=1, join_bursts=True)
    assert q["window"]["first_capture_id"] == 9
    with pytest.raises(ValueError, match="No bursts"):
        trace.prepare(session, tmp_path / "c.csv", start=1000, duration_s=1, join_bursts=True)


def test_survey_finds_playable_windows_and_dark(tmp_path):
    volts = fading(75, 1602)
    volts[10:40] = np.clip(volts[10:40], 0.6, 3.9)
    volts[:10] *= 3                                  # Saturated at the start
    volts[40:45] *= 3
    volts[45:75] = 0.0005                            # Beam blocked at the end
    r = trace.survey(FakeSession(volts, 0.711))
    assert r["windows"][0]["bursts"] == 30 and r["windows"][0]["joined_s"] == pytest.approx(30 * 0.6408)
    assert r["dark_candidates"][0]["bursts"] == 30 and abs(r["dark_candidates"][0]["mean_v"]) < 1e-3
    assert r["gaps"] == 74 and r["coverage"] == pytest.approx(0.6408 / 0.711, abs=0.01)


def test_sqlite_archive_is_read_read_only(tmp_path):
    store = Store(str(tmp_path))
    metadata = {"schema_version": 3, "settings": {"scope": {"volts_per_div": [1.0] * 4, "probe_factors": [1.0] * 4,
                                                            "sum_channel": 3, "sample_rate": 25000},
                                                  "calibration": {"minimum_sum_v": 0.5, "saturation_v": 4.0}}}
    with store.connect() as db:
        db.execute("INSERT INTO sessions(id,started_utc,status,mode,metadata) VALUES(?,?,?,?,?)",
                   ("s1", "2026-01-01T00:00:00+00:00", "complete", "hardware", json.dumps(metadata)))
    from oclab.decimation import CODE_SCALE, CODES_PER_DIV
    expected = []
    for k in range(3):
        sum_v = 1.0 + 0.1 * k + 0.01 * np.arange(374) / 374
        codes = np.zeros((4, 374), np.int16)
        codes[2] = np.rint(sum_v * CODES_PER_DIV * CODE_SCALE)
        expected.append(codes[2] / (CODES_PER_DIV * CODE_SCALE))
        store.save("s1", {"codes": codes, "clipped": np.zeros(374, bool), "store_rate_hz": RATE, "t0_s": 0.0},
                   {"host_started_ns": k * 500_000_000, "host_received_ns": k * 500_000_000 + 200_000_000, "sample_count": 374})
    path = tmp_path / "recordings.sqlite3"
    before = path.read_bytes()
    (session,) = sources.open_source(tmp_path)
    assert (session.rate_hz, session.samples_per_burst, session.bursts) == (RATE, 374, 3)
    volts, rails, clipped = session.read(0, 3)
    assert np.allclose(volts, expected) and rails is None and clipped.shape == (3, 374)
    with pytest.raises(sqlite3.OperationalError):    # The connection really is read-only
        with sources._connect(path) as db:
            db.execute("DELETE FROM captures")
    assert path.read_bytes() == before


# --------------------------------------------------------------------------
# Calibration and drive
# --------------------------------------------------------------------------

def make_calibration(points=26, noise=0.0, vmax=5.0):
    rng = np.random.default_rng(0)
    v = np.linspace(0, vmax, points)
    p = 100 * 10 ** (-(v / vmax) ** 1.5 * 4.2) * (1 + noise * rng.normal(size=points))   # 42 dB, nonlinear
    return v, p


def test_calibration_inverts_the_curve():
    v, p = make_calibration()
    cal = drive.Calibration(*drive.monotonic_curve(v, p)[:2], "uW")
    assert np.allclose(cal.voltage_for(p), v, atol=1e-9)
    mid = np.sqrt(p[5] * p[6])                            # Halfway in log power
    assert v[5] < cal.voltage_for(mid) < v[6]
    assert cal.range_db == pytest.approx(42, abs=0.1)


def test_noisy_calibration_is_made_monotonic_and_dark_points_dropped(tmp_path):
    v, p = make_calibration(noise=0.2)
    p[-3:] = [0.0, -0.01, 0.002]                         # At or below dark
    rows = [{"voltage_v": a, "code": int(a / 5 * 4096), "power": b, "monitor_v": None} for a, b in zip(v[::-1], p[::-1])]
    drive.save_calibration(tmp_path / "cal.csv", rows, {"unit": "uW", "vdd": 5.0})
    cal = drive.load_calibration(tmp_path / "cal.csv")
    assert np.all(np.diff(cal.voltage) > 0) and np.all(np.diff(cal.power) < 0)
    assert cal.voltage[-1] <= v[-4] and cal.dropped >= 3 and cal.unit == "uW" and cal.meta["vdd"] == 5.0
    volts = cal.voltage_for(np.geomspace(cal.power[-1], cal.power[0], 200))
    assert np.all(np.diff(volts) <= 1e-12)               # Single-valued inverse


def test_calibration_rejects_dbm_and_too_few_points():
    with pytest.raises(ValueError, match="not dBm"):
        drive.monotonic_curve([0, 1, 2, 3, 4, 5], [-10, -12, -15, -20, -25, -30])
    with pytest.raises(ValueError, match="usable points"):
        drive.monotonic_curve([0, 1, 2], [3, 2, 1])


def test_drive_clips_to_the_calibrated_range_and_reports_it():
    v, p = make_calibration()
    cal = drive.Calibration(*drive.monotonic_curve(v, p)[:2], "uW")
    h = np.array([0.5, 1.0, 1.5, 4.0, 1e-6, 1.0])
    mean = 30.0
    volts, target, stats = drive.plan_drive(h, cal, mean)
    assert target.max() == pytest.approx(0.95 * p[0]) and target.min() == pytest.approx(p[-1])
    assert stats["clipped_high_fraction"] == pytest.approx(1 / 6) and stats["clipped_low_fraction"] == pytest.approx(1 / 6)
    assert stats["mean_dbm"] == pytest.approx(10 * np.log10(mean / 1000))
    assert stats["scintillation_index_trace"] == pytest.approx(h.var() / h.mean() ** 2)
    assert np.all(np.diff(volts[[4, 0, 1, 2, 3]]) < 0)   # More power, less voltage (bright VOA)
    assert volts[3] > 0                                  # The surge stops at 95 %, short of 0 V
    _, _, fit = drive.plan_drive(h[:4], cal, None)
    assert fit["clipped_high_fraction"] == 0
    with pytest.raises(ValueError, match="outside the playable range"):
        drive.plan_drive(h, cal, 1000.0)


def test_codes_never_exceed_the_limit_even_for_malformed_input():
    for vdd in (4.75, 4.97, 5.0, 5.25):
        limit = drive.code_limit(vdd, 5.0)
        assert drive.codes_to_volts(limit, vdd) <= 5.0 and limit * vdd / 4096 <= 5.0
        assert limit == 4095 or drive.codes_to_volts(limit + 3, vdd) > 5.0
        codes = drive.volts_to_codes([-3, 0, 2.5, 5.0, 6.5, 1e9], vdd, 5.0)
        assert codes.max() <= limit and codes[0] == codes[1] == 0
    assert drive.volts_to_codes([4.0], 5.0, 3.0)[0] == drive.code_limit(5.0, 3.0)
    for bad in ([np.nan], [np.inf], [1.0, -np.inf]):
        with pytest.raises(ValueError, match="non-finite"):
            drive.volts_to_codes(bad, 5.0, 5.0)
    for vdd, vmax in ((5.0, 5.5), (5.0, 6.5), (5.0, 0.0), (9.0, 5.0)):
        with pytest.raises(ValueError):
            drive.code_limit(vdd, vmax)


def test_zero_code_is_a_true_zero():
    assert drive.pwm_level([0, 1, 2, 5, 6, 4093, 4094, 4095]).tolist() == [0, 0, 1, 1, 2, 1023, 1024, 1024]


# --------------------------------------------------------------------------
# Serial protocol (no hardware)
# --------------------------------------------------------------------------

class FakeLink:
    def __init__(self, replies=b"K", monitor=True):
        self.incoming = bytearray(replies)
        self.sent = bytearray()
        self.monitor = monitor

    def read(self, n):
        out, self.incoming = bytes(self.incoming[:n]), self.incoming[n:]
        return out

    def write(self, data):
        self.sent += data
        if data == b"Q" and self.monitor:
            self.incoming += bytes([ord("Q"), 0x80 | (8000 >> 7), 8000 & 0x7F])

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.incoming.clear()

    def close(self):
        pass


def test_decoder_separates_monitor_credits_and_report_split_anywhere():
    stream = bytes([0x80 | (1000 >> 7), 1000 & 0x7F]) + b"C" + bytes([0x80, 5]) + b"Z448 0 0 16 0\r\n"
    for cut in range(len(stream) + 1):
        d = board.MonitorDecoder()
        d.feed(stream[:cut])
        d.feed(stream[cut:])
        assert d.monitor == [1000, 5] and d.credits == 1 and d.unexpected == 0
        assert d.report == {"played": 448, "underruns": 0, "i2c_errors": 0, "max_late_us": 16, "monitor_dropped": 0}


def test_board_refuses_codes_above_the_limit_and_sends_nothing():
    link = FakeLink()
    b = board.Board("fake", limit=4000, boot_wait_s=0, link=link)
    link.sent.clear()
    for code in (4001, -1):
        with pytest.raises(ValueError):
            b.set_code(code)
    for codes in (np.array([1.5]), np.array([0, 10, 4096]), np.array([], dtype=int)):
        with pytest.raises(ValueError):
            b.play(codes)
    assert link.sent == b""
    b.set_code(4000)
    assert link.sent == b"V\xa0\x0f"


def test_wrong_sketch_and_missing_dac_are_reported():
    with pytest.raises(board.WrongSketch, match="different sketch.*Hello"):
        board.Board("fake", 4095, boot_wait_s=0, link=FakeLink(b"Hello world\r\n"))
    with pytest.raises(RuntimeError, match="MCP4725"):
        board.Board("fake", 4095, boot_wait_s=0, link=FakeLink(b"E"))
    # Version 1 of the streamer: answers R with K, but has no monitor query (seen on the lab board).
    with pytest.raises(board.WrongSketch, match="older sketch without the A0 monitor"):
        board.Board("fake", 4095, boot_wait_s=0, link=FakeLink(b"K", monitor=False))
    b = board.Board("fake", 4095, boot_wait_s=0, link=FakeLink(b"K"))
    assert b.query(2) == 500.0


# --------------------------------------------------------------------------
# Command line without hardware
# --------------------------------------------------------------------------

def write_trace(path, h, sum_v=None):
    sum_v = 2.0 * h if sum_v is None else sum_v
    with open(path, "w") as f:
        f.write("t_s,h,sum_v,flagged\n")
        for k, (a, b) in enumerate(zip(h, sum_v)):
            f.write(f"{k / 1000:.3f},{a:.6f},{b:.5f},0\n")


def write_calibration(path, unit="uW", monitor_dark=0.012):
    v, p = make_calibration()
    rows = [{"voltage_v": a, "code": int(a / 5 * 4096), "power": b, "monitor_v": None} for a, b in zip(v, p)]
    drive.save_calibration(path, rows, {"unit": unit, "vdd": 5.0, "monitor_dark_v": monitor_dark})


def test_play_refuses_without_calibration_and_dry_run_sends_nothing(tmp_path, capsys):
    h = np.exp(0.3 * np.random.default_rng(1).normal(size=2000))
    h /= h.mean()
    write_trace(tmp_path / "t.csv", h)
    with pytest.raises(SystemExit, match="Refusing to play without a calibration"):
        cli.main(["play", str(tmp_path / "t.csv"), "--dry-run"])
    write_calibration(tmp_path / "cal.csv")
    cli.main(["--port", "/nonexistent", "play", str(tmp_path / "t.csv"), "--cal", str(tmp_path / "cal.csv"),
              "--dry-run", "--save-drive", str(tmp_path / "codes.txt")])
    out = capsys.readouterr().out
    assert "-15.0 dBm" in out and "Dry run" in out and "Clipped" in out
    codes = np.loadtxt(tmp_path / "codes.txt", skiprows=1)
    assert codes.size == h.size and codes.max() <= drive.code_limit(5.0, 5.0)
    with pytest.raises(SystemExit):
        cli.main(["--vmax", "6", "play", str(tmp_path / "t.csv"), "--direct", "--dry-run"])


def test_validate_identical_monitor_gives_zero_error(tmp_path, capsys):
    rng = np.random.default_rng(4)
    target = 30 * np.exp(np.convolve(0.4 * rng.normal(size=6000), np.ones(20) / 20, "same"))
    dark = 0.05
    monitor = np.roll(0.001 * target, 1) + dark                  # Scaled copy, 1 ms late, on a dark offset
    with open(tmp_path / "run.csv", "w") as f:
        f.write("index,dac_code,drive_v,target_power,monitor_v,monitor_clipped\n")
        for k in range(target.size):
            f.write(f"{k},100,0.1,{target[k]:.6g},{monitor[k]:.6f},0\n")
    (tmp_path / "run.json").write_text(json.dumps({"accepted": True, "calibration": {"meta": {"monitor_dark_v": dark}}}))
    cli.main(["validate", str(tmp_path / "run.csv"), "--out", str(tmp_path / "v.json")])
    r = json.loads((tmp_path / "v.json").read_text())
    assert r["lag_samples"] == 1 and r["correlation"] > 0.999
    assert abs(r["error_db"]["mean"]) < 0.01 and r["error_db"]["rms"] < 0.02
    assert r["scintillation_index"]["target"] == pytest.approx(r["scintillation_index"]["monitor"], rel=1e-3)
    assert "Kolmogorov" in capsys.readouterr().out


def test_fade_statistics_count_fades():
    h = np.ones(1000)
    h[100:110] = 0.4      # 10 ms at -4 dB
    h[500:530] = 0.05     # 30 ms at -13 dB
    stats = {s["threshold_db"]: s for s in analysis.fade_statistics(h, 1000)}
    assert stats[-3.0]["fades_per_s"] == 2 and stats[-3.0]["mean_duration_ms"] == pytest.approx(20)
    assert stats[-10.0]["fades_per_s"] == 1 and stats[-10.0]["max_duration_ms"] == pytest.approx(30)
