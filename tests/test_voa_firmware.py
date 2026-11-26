"""End to end against the real sketch, compiled for the host with a mocked Arduino (tests/voa_sim).

oclab-voa talks to the simulated board over a pseudo-terminal at real serial
speed. The simulation logs the output level the sketch measures at every tick,
so the test can check that every sample was played in order and that every
logged monitor value belongs to the right output sample.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess

import numpy as np
import pytest

from oclab.voa import board, cli, drive

ROOT = Path(__file__).parents[1]
CXX = shutil.which("g++") or shutil.which("clang++")
pytestmark = pytest.mark.skipif(CXX is None or os.name != "posix", reason="needs a C++ compiler and a POSIX pty")


@pytest.fixture(scope="module")
def simulator(tmp_path_factory):
    binary = tmp_path_factory.mktemp("sim") / "voa_sim"
    subprocess.run([CXX, "-std=c++17", "-O2", "-Wall", "-Werror", "-I", str(ROOT / "tests/voa_sim"), "-include", "Arduino.h",
                    "-x", "c++", str(ROOT / "firmware/voa_player/voa_player.ino"),
                    "-x", "c++", str(ROOT / "tests/voa_sim/sim.cpp"), "-o", str(binary)], check=True)
    return binary


class Board:
    def __init__(self, binary, events, light="identity"):
        self.events = events
        self.process = subprocess.Popen([str(binary), str(events), light], stdout=subprocess.PIPE)
        self.port = self.process.stdout.readline().decode().strip()

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(10)
        levels, d8, end = [], [], None
        for line in Path(self.events).read_text().splitlines():
            kind, *values = line.split()
            if kind == "L":
                levels.append(int(values[0]))
            elif kind == "D8":
                d8.append((int(values[0]), int(values[1])))
            elif kind == "END":
                end = tuple(map(int, values))
        return np.array(levels), d8, end


@pytest.fixture
def sim(simulator, tmp_path, monkeypatch):
    monkeypatch.setattr(board, "BOOT_WAIT_S", 0.2)
    boards = []

    def start(light="identity"):
        b = Board(simulator, tmp_path / f"events{len(boards)}.txt", light)
        boards.append(b)
        return b
    yield start
    for b in boards:
        b.stop()


def test_every_sample_is_played_in_order_and_every_monitor_sample_lines_up(sim, tmp_path):
    rng = np.random.default_rng(7)
    n = 3000
    h = np.exp(np.convolve(0.5 * rng.normal(size=n), np.ones(15) / 15, "same"))
    h /= h.mean()
    h[100:140] = 1e-6                                     # Deep fade: clipped at the deepest calibrated point
    h[200:230] = 50                                       # Surge: clipped at 95 % of the 0 V transmission
    trace_path = tmp_path / "t.csv"
    with open(trace_path, "w") as f:
        f.write("t_s,h,sum_v,flagged\n")
        f.writelines(f"{k / 1000:.3f},{v:.6f},{2 * v:.5f},0\n" for k, v in enumerate(h))
    v = np.linspace(0, 5, 26)
    rows = [{"voltage_v": a, "code": 0, "power": 100 * 10 ** (-a * 0.8), "monitor_v": None} for a in v]
    drive.save_calibration(tmp_path / "cal.csv", rows, {"unit": "uW", "monitor_dark_v": 0.0})

    b = sim()
    log = tmp_path / "run.csv"
    vdd = 4.97
    cli.main(["--port", b.port, "--vdd", str(vdd), "play", str(trace_path), "--cal", str(tmp_path / "cal.csv"),
              "--mean-dbm", "-15", "--hold", "1.25", "--log", str(log)])
    levels, d8, end = b.stop()

    data = np.genfromtxt(log, delimiter=",", names=True)
    codes = data["dac_code"].astype(int)
    expected = drive.pwm_level(codes)
    hold_level = drive.pwm_level(drive.volts_to_codes([1.25], vdd, 5.0))[0]
    assert codes.size == n and codes.max() <= drive.code_limit(vdd, 5.0)
    # The sketch measures before each update: read 0 sees the level set before 'G' (the first code), read k the
    # level of sample k-1, and the final read the last trace sample (the hold sample is played after it).
    assert levels.size == n + 1
    assert levels[0] == expected[0] and np.array_equal(levels[1:], expected)
    assert end == (hold_level, 0)
    # Monitor column k is A0 read 1 ms after sample k was applied (identity light model: A0 = level).
    assert np.allclose(data["monitor_v"], np.minimum(expected, 1023) * vdd / 1024, atol=6e-5)
    # D8 rises at the first played sample and falls at the end of playback.
    assert d8 == [(1, 0), (0, n + 1)]
    run = json.loads(log.with_suffix(".json").read_text())
    assert run["accepted"] and run["board_report"]["played"] == n + 1
    assert run["board_report"]["underruns"] == 0 and run["board_report"]["monitor_dropped"] == 0
    assert run["monitor_samples"] == n + 1 and run["unexpected_bytes"] == 0
    assert run["drive"]["clipped_low_fraction"] > 0 and run["drive"]["clipped_high_fraction"] > 0
    assert run["started_utc"] < run["ended_utc"]


def test_direct_bench_mode_copies_volts_within_the_limit(sim, tmp_path):
    sum_v = np.concatenate([np.linspace(0, 6, 500), np.full(100, 0.0)])     # Past the 5 V limit on purpose
    with open(tmp_path / "t.csv", "w") as f:
        f.write("t_s,h,sum_v,flagged\n")
        f.writelines(f"{k / 1000:.3f},1.0,{v:.5f},0\n" for k, v in enumerate(sum_v))
    b = sim()
    cli.main(["--port", b.port, "play", str(tmp_path / "t.csv"), "--direct", "--log", str(tmp_path / "run.csv")])
    levels, _, end = b.stop()
    codes = np.genfromtxt(tmp_path / "run.csv", delimiter=",", names=True)["dac_code"].astype(int)
    assert np.array_equal(codes, drive.volts_to_codes(sum_v, 5.0, 5.0))
    assert drive.codes_to_volts(codes, 5.0).max() <= 5.0 and np.array_equal(levels[1:], drive.pwm_level(codes))
    assert end == (0, 0)                                  # Default hold for --direct: 0 V


def test_calibrate_with_the_monitor_takes_dark_first(sim, tmp_path, capsys):
    b = sim("bright")
    blocked = []

    def ask(prompt):
        if prompt.startswith(("Block", "Restore")):
            os.kill(b.process.pid, signal.SIGUSR1)       # Toggle the simulated beam block
            blocked.append(prompt.split()[0])
        return ""
    cli.main(["--port", b.port, "calibrate", "--monitor", "--steps", "11", "--settle", "0.02",
              "--out", str(tmp_path / "cal.csv")], ask=ask)
    _, _, end = b.stop()
    assert blocked == ["Block", "Restore"] and end[0] == 0          # Back to 0 V afterwards
    cal = drive.load_calibration(tmp_path / "cal.csv")
    assert cal.unit == "monitor_v" and cal.meta["monitor_dark_v"] == pytest.approx(2 * 5 / 1024)
    assert np.all(np.diff(cal.power) < 0) and cal.range_db > 20
    assert "Usable range" in capsys.readouterr().out


def test_set_holds_a_voltage_and_reads_the_monitor(sim, capsys):
    b = sim()
    cli.main(["--port", b.port, "set", "2.5"])
    _, _, end = b.stop()
    assert end == (512, 0)
    assert "Code 2048: output 2.500 V. A0 monitor 512.0 of 1023 = 2.5000 V" in capsys.readouterr().out
