"""Raspberry Pi Pico W weather station on USB CDC serial.

The firmware's line protocol is fixed; see README "Weather station".
Acquisition runs in its own thread so a scope failure never stops weather
logging and a serial failure never stops scope capture.
"""
from __future__ import annotations

from collections import deque
import logging
import math
import shutil
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

PICO_VID = 0x2E8A
WIND_FIELDS = ("v_adc", "i_ma", "wind_ms", "clip")
ENV_FIELDS = ("dht_c", "dht_rh", "baro_hpa", "baro_c", "light_v", "light_x")
ASSOCIATION_FIELDS = (
    "wind_mean_ms", "wind_std_ms", "wind_max_ms", "wind_n", "wind_ti", "taylor_ok",
    "air_temp_c", "dht_temp_c", "rh_pct", "pressure_hpa", "light_v", "ldr_ohm",
    "weather_age_s", "weather_status",
)
SENSORS = [
    {"quantity": "wind speed", "sensor": "cup anemometer, 4-20 mA loop into the Pico ADC via a shunt",
     "unit": "m/s", "rate": "10 Hz", "notes": "Slow response (distance constant about 2-3 m); not turbulence-resolving. clip=1 marks ADC saturation."},
    {"quantity": "air temperature", "sensor": "BMP280", "unit": "degC", "rate": "1 Hz", "notes": "Reference air-temperature sensor (air_temp_c)."},
    {"quantity": "station pressure", "sensor": "BMP280", "unit": "hPa", "rate": "1 Hz", "notes": "Not reduced to sea level."},
    {"quantity": "temperature, relative humidity", "sensor": "DHT11", "unit": "degC, %RH", "rate": "1 Hz",
     "notes": "Integer resolution; accuracy about +/-2 degC and +/-5 %RH."},
    {"quantity": "light", "sensor": "LDR (light=ldr) or BPW34 photodiode (light=bpw34)", "unit": "V; ohm (LDR) or uA (BPW34)",
     "rate": "1 Hz", "notes": "Uncalibrated relative light level."},
]
CAVEATS = [
    "The cup anemometer has a distance constant of about 2-3 m, so wind statistics are not turbulence-resolving.",
    "Cn2 is not measured by any of these sensors.",
    "The BMP280 temperature is the air-temperature sensor; the DHT11 is accurate to about +/-2 degC and +/-5 %RH.",
    "Empty firmware fields mean a missing sensor or failed read and are stored as null, never 0.",
    "Times are host receive timestamps (time.time_ns on line receipt), including USB/serial latency. Pico t_ms is an auxiliary uptime counter that wraps.",
]


class ParseError(ValueError):
    pass


def _number(text, kind=float):
    """Empty means missing (None). NaN/Inf from a failed read are also missing."""
    text = text.strip()
    if not text:
        return None
    try:
        value = kind(text)
    except ValueError:
        raise ParseError(f"Invalid number {text!r}") from None
    if kind is float and not math.isfinite(value):
        return None
    return value


def parse_header(line: str) -> dict | None:
    """'# pico-weather v2.1 wind=10Hz ...' -> {'firmware': 'pico-weather', 'version': 'v2.1', 'wind': '10Hz', ...}."""
    words = line.lstrip("#").split()
    pairs = dict(word.split("=", 1) for word in words if "=" in word and not word.startswith("="))
    if not pairs:
        return None
    names = [word for word in words if "=" not in word]
    header = {}
    if names:
        header["firmware"] = names[0]
    if len(names) > 1:
        header["version"] = names[1]
    return {**header, **pairs}


def parse_line(line: str):
    """Return ('W'|'E', fields), ('header', dict) or ('comment', text). Raise ParseError otherwise."""
    line = line.strip()
    if not line:
        raise ParseError("Empty line")
    if line.startswith("#"):
        header = parse_header(line)
        return ("header", header) if header else ("comment", line)
    parts = line.split(",")
    kind = parts[0]
    if kind == "W":
        if len(parts) != 6:
            raise ParseError(f"W line has {len(parts)} fields, expected 6")
        fields = dict(zip(("v_adc", "i_ma", "wind_ms"), (_number(p) for p in parts[2:5])))
        clip = _number(parts[5], int)
        if clip not in (None, 0, 1):
            raise ParseError("clip must be 0 or 1")
        fields["clip"] = clip
    elif kind == "E":
        if len(parts) != 8:
            raise ParseError(f"E line has {len(parts)} fields, expected 8")
        fields = dict(zip(ENV_FIELDS, (_number(p) for p in parts[2:])))
    else:
        raise ParseError(f"Unknown line type {kind[:8]!r}")
    t_ms = _number(parts[1], int)
    if t_ms is None or t_ms < 0:
        raise ParseError("Missing Pico t_ms")
    return kind, {"t_ms": t_ms, **fields}


def associate(wind, env, end_ns: int, window_s: float, stale_after_s: float) -> dict:
    """Weather fields for a capture received at end_ns.

    Wind statistics use samples with end_ns - window < host time <= end_ns.
    Environment values are the latest E sample received at or before end_ns.
    """
    start_ns = end_ns - round(window_s * 1e9)
    in_window = [w for w in wind if start_ns < w["host_received_ns"] <= end_ns]
    speeds = np.array([w["wind_ms"] for w in in_window if w["wind_ms"] is not None], dtype=float)
    result = dict.fromkeys(ASSOCIATION_FIELDS)
    result["wind_n"] = len(speeds)
    if len(speeds):
        mean, std = float(speeds.mean()), float(speeds.std())
        result.update(wind_mean_ms=mean, wind_std_ms=std, wind_max_ms=float(speeds.max()))
        result["wind_ti"] = std / mean if mean >= 1 else None
        result["taylor_ok"] = mean >= 1 and result["wind_ti"] < 0.5
    latest_env = max((e for e in env if e["host_received_ns"] <= end_ns), key=lambda e: e["host_received_ns"], default=None)
    if latest_env:
        result.update(air_temp_c=latest_env["baro_c"], dht_temp_c=latest_env["dht_c"], rh_pct=latest_env["dht_rh"],
                      pressure_hpa=latest_env["baro_hpa"], light_v=latest_env["light_v"], ldr_ohm=latest_env["light_x"],
                      weather_age_s=(end_ns - latest_env["host_received_ns"]) / 1e9)
    last_wind = max((w["host_received_ns"] for w in in_window), default=None)
    if last_wind is None and latest_env is None:
        result["weather_status"] = "missing"
    elif (last_wind is not None and (end_ns - last_wind) / 1e9 <= stale_after_s
          and result["weather_age_s"] is not None and result["weather_age_s"] <= stale_after_s):
        result["weather_status"] = "ok"
    else:
        result["weather_status"] = "stale"
    return result


def unavailable(status: str) -> dict:
    return {**dict.fromkeys(ASSOCIATION_FIELDS), "wind_n": 0, "weather_status": status}


class SerialWeatherSource:
    """Line reader for the Pico's USB CDC port. read_line() returns None on timeout."""

    def __init__(self, settings):
        self.settings = settings
        self.serial = None
        self.port = None
        self.pending = b""

    def open(self):
        import serial
        from serial.tools import list_ports
        port = self.settings.weather.port
        if port is None:
            found = sorted(p.device for p in list_ports.comports() if p.vid == PICO_VID)
            if not found:
                raise OSError("No Pico (USB VID 0x2E8A) serial port found")
            if len(found) > 1:
                logger.warning("Several Pico serial ports found %s; using %s. Set [weather].port to choose.", found, found[0])
            port = found[0]
        self.serial = serial.Serial(port, self.settings.weather.baud, timeout=0.5)
        self.port, self.pending = port, b""

    def read_line(self) -> str | None:
        # readline() returns a partial line on timeout; keep it until its newline arrives.
        data = self.serial.readline()
        if not data:
            return None
        if not data.endswith(b"\n"):
            self.pending += data
            if len(self.pending) > 4096:
                self.pending = b""
                return "\x00overlong"  # Counted as malformed by the recorder.
            return None
        data, self.pending = self.pending + data, b""
        return data.decode("ascii", errors="replace")

    def close(self):
        if self.serial is not None:
            try:
                self.serial.close()
            finally:
                self.serial = None


class DemoWeatherSource:
    """Synthetic firmware lines; only used in demo mode, never as a hardware fallback."""

    port = "demo"

    def __init__(self, settings):
        self.rng = np.random.default_rng()
        self.queue = deque()
        self.next_at = None
        self.started = time.monotonic()

    def open(self):
        self.queue.append("# pico-weather demo wind=10Hz anemo=4-20mA shunt=165.0ohm range=30.0m/s light=ldr baro=BMP280 dht=yes")
        self.next_at, self.tick = time.monotonic(), 0

    def read_line(self) -> str | None:
        if self.queue:
            return self.queue.popleft()
        wait = self.next_at - time.monotonic()
        if wait > 0:
            time.sleep(min(wait, 0.5))
            return None
        self.next_at += 0.1
        self.tick += 1
        t_ms = int((time.monotonic() - self.started) * 1000)
        phase = time.monotonic() - self.started
        wind = max(0.0, 3.2 + 1.1 * math.sin(phase / 40) + self.rng.normal(0, 0.45))
        i_ma = 4 + wind / 30 * 16
        self.queue.append(f"W,{t_ms},{i_ma * 0.165:.4f},{i_ma:.3f},{wind:.3f},0")
        if self.tick % 10 == 0:
            light = 1.6 + 0.2 * math.sin(phase / 300)
            self.queue.append(f"E,{t_ms},{round(19 + math.sin(phase / 600))},{round(41 + 3 * math.sin(phase / 500))},"
                              f"{835.2 + 0.3 * math.sin(phase / 900):.2f},{18.6 + math.sin(phase / 600):.2f},"
                              f"{light:.3f},{10000 * (3.3 - light) / light:.0f}")
        return self.queue.popleft()

    def close(self):
        self.queue.clear()


class WeatherRecorder:
    """Owns the weather thread, its live state and batched persistence."""

    def __init__(self, settings, store, source=None):
        self.settings = settings
        self.store = store
        self.enabled = settings.weather.enabled
        self.source = source or (DemoWeatherSource(settings) if settings.mode == "demo" else SerialWeatherSource(settings))
        self.stop_event = threading.Event()
        self.mutex = threading.Lock()
        self.thread = None
        self.state = "disabled" if not self.enabled else "starting"
        self.message = "Weather station disabled in configuration" if not self.enabled else "Connecting to weather station"
        self.port = None
        self.header = None
        self.latest = {"W": None, "E": None}
        self.received_at = {"W": None, "E": None}  # monotonic receipt times for live ages
        keep_s = max(settings.weather.association_window_s, 1) + 5
        self.keep_ns = round(keep_s * 1e9)
        self.recent = {"W": deque(), "E": deque()}
        self.pending = {"W": [], "E": [], "header": []}
        self.committed_through_ns = None
        self.counters = dict(lines=0, wind=0, env=0, headers=0, comments=0, malformed=0,
                             connection_errors=0, write_errors=0, dropped=0)

    # Lifecycle ------------------------------------------------------------
    def start(self):
        if self.enabled:
            self.thread = threading.Thread(target=self.run, name="oclab-weather", daemon=True)
            self.thread.start()

    def stop(self) -> bool:
        """Return False if the thread did not stop (the caller then keeps the process lock)."""
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                logger.error("Weather worker did not stop within 10 seconds")
                return False
        return True

    def set_status(self, state, message):
        with self.mutex:
            self.state, self.message = state, message

    def run(self):
        last_flush = time.monotonic()
        silence_limit = max(2 * self.settings.weather.stale_after_s, 5)
        try:
            while not self.stop_event.is_set():
                try:
                    self.source.open()
                    with self.mutex:
                        self.port = getattr(self.source, "port", None)
                        self.state, self.message = "connected", "Weather station connected; waiting for data"
                    last_line = time.monotonic()
                    while not self.stop_event.is_set():
                        line = self.source.read_line()
                        received_ns = time.time_ns()
                        now = time.monotonic()
                        if line is not None:
                            last_line = now
                            self.handle(line, received_ns, now)
                        elif now - last_line > silence_limit:
                            raise TimeoutError(f"No weather data for {silence_limit:.0f} s")
                        if now - last_flush >= 1:
                            self.flush()
                            last_flush = now
                except Exception as exc:
                    logger.warning("Weather station unavailable (%s); retrying", exc)
                    with self.mutex:
                        self.counters["connection_errors"] += 1
                        self.state = "disconnected"
                        self.message = "Weather station unavailable. Check USB, dialout group membership and that Thonny is closed; reconnecting automatically."
                    self.close_source()
                    self.flush()
                    self.stop_event.wait(self.settings.retry_interval_s)
        finally:
            self.close_source()
            self.flush()
            self.set_status("stopped", "Weather recorder stopped")

    def close_source(self):
        try:
            self.source.close()
        except Exception:
            logger.exception("Error closing weather source")

    # Line handling ----------------------------------------------------------
    def handle(self, line: str, received_ns: int, now: float):
        raw = line.strip()
        try:
            kind, fields = parse_line(raw)
        except ParseError:
            with self.mutex:
                self.counters["lines"] += 1
                self.counters["malformed"] += 1
            return
        with self.mutex:
            self.counters["lines"] += 1
            if kind == "comment":
                self.counters["comments"] += 1
                return
            if kind == "header":
                self.counters["headers"] += 1
                self.header = fields
                self.pending["header"].append({"host_received_ns": received_ns, "header": fields, "raw": raw})
                return
            sample = {"host_received_ns": received_ns, **fields, "raw": raw}
            self.counters["wind" if kind == "W" else "env"] += 1
            self.latest[kind], self.received_at[kind] = sample, now
            recent = self.recent[kind]
            recent.append(sample)
            while recent and recent[0]["host_received_ns"] < received_ns - self.keep_ns:
                recent.popleft()
            self.pending[kind].append(sample)
            if self.state != "storage_error":
                self.state, self.message = "receiving", "Weather station data received"

    def flush(self):
        with self.mutex:
            batch, self.pending = self.pending, {"W": [], "E": [], "header": []}
        if not any(batch.values()):
            return
        try:
            if shutil.disk_usage(self.store.root).free / 1024**2 < self.settings.minimum_free_disk_mb:
                raise OSError("Disk space is below the configured reserve")
            self.store.save_weather(self.settings.mode, batch["W"], batch["E"], batch["header"])
        except Exception as exc:
            logger.error("Weather write failed: %s", exc)
            with self.mutex:
                self.counters["write_errors"] += 1
                # Keep at most about 10 minutes of unsaved samples, then drop the oldest.
                for kind in batch:
                    merged = batch[kind] + self.pending[kind]
                    excess = max(0, len(merged) - 6000)
                    self.counters["dropped"] += excess
                    self.pending[kind] = merged[excess:]
                self.state, self.message = "storage_error", "Weather data cannot be saved. Check disk and service logs."
            return
        newest = max((s["host_received_ns"] for s in batch["W"] + batch["E"]), default=None)
        with self.mutex:
            if newest is not None:
                self.committed_through_ns = max(newest, self.committed_through_ns or 0)
            if self.state == "storage_error":
                self.state, self.message = "receiving", "Weather station data received"

    # Views ------------------------------------------------------------------
    def associate(self, end_ns: int) -> dict:
        if not self.enabled:
            return unavailable("disabled")
        w = self.settings.weather
        with self.mutex:
            wind, env = list(self.recent["W"]), list(self.recent["E"])
            if self.latest["E"] and (not env or env[-1] is not self.latest["E"]):
                env.append(self.latest["E"])  # Older than the buffer: still reported, as stale.
        return associate(wind, env, end_ns, w.association_window_s, w.stale_after_s)

    def snapshot(self) -> dict:
        stale_after = self.settings.weather.stale_after_s
        with self.mutex:
            now = time.monotonic()
            ages = {k: None if t is None else now - t for k, t in self.received_at.items()}
            cutoff = time.time_ns() - 1_000_000_000
            last_second = [s["wind_ms"] for s in self.recent["W"] if s["host_received_ns"] > cutoff and s["wind_ms"] is not None]
            strip = lambda s: None if s is None else {k: v for k, v in s.items() if k != "raw"}
            fresh = all(a is not None and a <= stale_after for a in ages.values())
            return {
                "enabled": self.enabled, "state": self.state, "message": self.message,
                "source": self.settings.mode, "port": self.port, "header": self.header,
                "stale": not fresh, "wind_age_s": ages["W"], "env_age_s": ages["E"],
                "age_s": max(ages.values()) if None not in ages.values() else None,
                "wind": strip(self.latest["W"]), "env": strip(self.latest["E"]),
                "wind_last_1s": {"mean_ms": sum(last_second) / len(last_second), "max_ms": max(last_second),
                                 "n": len(last_second), "t_ns": cutoff + 1_000_000_000} if last_second else None,
                "counters": dict(self.counters), "stale_after_s": stale_after,
            }

    def metadata(self) -> dict:
        w = self.settings.weather
        with self.mutex:
            header, port = self.header, self.port
        return {
            "enabled": self.enabled, "source": self.settings.mode,
            "device": {"description": "Raspberry Pi Pico W weather station, USB CDC serial", "usb_vid": "0x2E8A",
                       "port": port, "configured_port": w.port, "baud": w.baud},
            "header": header,
            "header_note": "Parsed from the firmware's '# ...' header line. If it arrives after the session starts it is filled in when the session ends; null if none was received (for example, the Pico booted before the recorder connected). Every header seen is stored in the weather_headers table.",
            "line_format": {"W": "W,t_ms,v_adc,i_ma,wind_ms,clip (10 Hz)",
                            "E": "E,t_ms,dht_c,dht_rh,baro_hpa,baro_c,light_v,light_x (1 Hz)"},
            "sensors": SENSORS,
            "association": {
                "window_s": w.association_window_s, "stale_after_s": w.stale_after_s,
                "rule": "Wind statistics use W samples with capture host_received_ns - window_s < host time <= host_received_ns; population std. wind_ti = std/mean, null when mean < 1 m/s. taylor_ok = mean >= 1 m/s and TI < 0.5. Environment fields are the latest E sample at or before the capture; weather_age_s is its age. weather_status: ok (wind and environment both within stale_after_s), stale, missing (no samples), disabled.",
                "fields": {"air_temp_c": "BMP280 temperature", "dht_temp_c": "DHT11 temperature", "rh_pct": "DHT11 relative humidity",
                           "pressure_hpa": "BMP280 station pressure", "light_v": "light-sensor voltage",
                           "ldr_ohm": "firmware light_x: LDR resistance (ohm) when light=ldr, photocurrent (uA) when light=bpw34"},
            },
            "caveats": CAVEATS,
        }
