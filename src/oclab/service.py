from __future__ import annotations

from collections import deque
import fcntl
import logging
import shutil
import threading
import time

from .acquisition import CalibrationUnavailable, DemoSource, HardwareSource, analyse, decimate_capture
from .config import Settings
from .storage import Store, ns_to_utc
from .weather import WeatherRecorder, unavailable

logger = logging.getLogger(__name__)


class Recorder:
    def __init__(self, settings: Settings, source=None, weather_source=None):
        self.settings = settings
        self.store = Store(settings.data_dir)
        self.source = source or (DemoSource(settings) if settings.mode == "demo" else HardwareSource(settings))
        # Separate thread and state; shares only the store and the process lock.
        self.weather = WeatherRecorder(settings, self.store, weather_source)
        # Whether captures (and weather) are saved; otherwise they are only shown live.
        self.logging = settings.record_on_start
        self.weather.logging = self.logging
        self.seq = 0  # Increments for every published capture, saved or not (live-stream replay)
        self.stop_event = threading.Event()
        self.mutex = threading.Lock()
        self.latest = None
        self.session_id = None
        self.state = "starting"
        self.message = "Connecting to acquisition source"
        self.last_success = None
        self.free_disk_mb = None
        self.thread = None
        self.file_lock = None
        self.recent = deque(maxlen=600)

    def start(self):
        self.file_lock = (self.store.root / ".recorder.lock").open("a")
        try:
            fcntl.flock(self.file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file_lock.close()
            raise RuntimeError("Another recorder is using this data directory. Run exactly one worker.") from exc
        try:
            self.store.recover()
            self.weather.start()
            self.thread = threading.Thread(target=self.run, name="oclab-recorder", daemon=True)
            self.thread.start()
        except Exception:
            self.weather.stop()
            self.file_lock.close()
            raise

    def stop(self):
        self.stop_event.set()
        weather_stopped = self.weather.stop()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                # Keep the process lock held; systemd will kill a stuck USB operation.
                logger.error("USB worker did not stop within 30 seconds")
                return
        if not weather_stopped:
            return
        if self.file_lock:
            self.file_lock.close()

    def set_status(self, state, message):
        with self.mutex:
            self.state, self.message = state, message

    def set_logging(self, enabled: bool):
        """Switch saving on or off. The capture loop opens/closes the session on its next burst."""
        with self.mutex:
            self.logging = enabled
            if self.state in ("recording", "monitoring"):
                self.state = "recording" if enabled else "monitoring"
                self.message = self.status_message(enabled)
        self.weather.set_logging(enabled)
        logger.info("Data logging %s", "started" if enabled else "stopped")

    def status_message(self, logging):
        if not logging:
            return "Live view only: data logging is off"
        return "Synthetic data is being recorded" if self.settings.mode == "demo" else "Acquiring and saving to disk"

    def snapshot(self):
        with self.mutex:
            age = time.monotonic() - self.last_success if self.last_success is not None else None
            stale = age is None or age > max(5, self.settings.capture_interval_s * 3)
            acquiring = self.state in ("recording", "monitoring") and not stale
            return {"status": {
                "state": self.state, "message": self.message, "mode": self.settings.mode,
                "stale": stale, "last_capture_age_s": age, "session_id": self.session_id,
                "acquiring": acquiring, "logging": self.logging,
                "recording": self.state == "recording" and not stale,
                "free_disk_mb": self.free_disk_mb,
            }, "latest": self.latest, "weather": self.weather.snapshot()}

    def captures_since(self, seq):
        with self.mutex:
            return [s for s in self.recent if s["seq"] > seq]

    def close_source(self):
        try:
            self.source.close()
        except Exception:
            logger.exception("Error closing acquisition source")

    def end_session(self, status="complete"):
        if self.session_id:
            with self.weather.mutex:
                header = self.weather.header
            self.store.finish_session(self.session_id, status, header)
            self.session_id = None

    def run(self):
        session_started = 0
        previous_start = None
        try:
            while not self.stop_event.is_set():
                start = time.monotonic()
                try:
                    with self.mutex:
                        logging = self.logging
                    if not logging:
                        self.end_session()  # Logging switched off: close the session cleanly
                    free = shutil.disk_usage(self.store.root).free / 1024**2
                    with self.mutex:
                        self.free_disk_mb = free
                    if logging and free < self.settings.minimum_free_disk_mb:
                        self.set_status("storage_error", "Recording paused: disk space is below the configured reserve")
                        self.close_source()
                        self.end_session("interrupted")
                        self.stop_event.wait(self.settings.retry_interval_s)
                        continue
                    started_ns = time.time_ns()
                    try:
                        capture = self.source.capture()
                        received_ns = time.time_ns()
                        elapsed = time.monotonic() - start
                        summary, preview = analyse(capture, self.settings)
                        stored, decimated = decimate_capture(capture, self.settings)
                        summary.update(decimated)
                    except CalibrationUnavailable:
                        logger.error("Scope zero-level calibration unavailable; not recording. Retrying.")
                        self.set_status("disconnected", "Oscilloscope calibration could not be read, so readings would be offset. Not recording; retrying automatically. Replug the scope if this persists.")
                        self.close_source()
                        self.end_session("interrupted")
                        self.stop_event.wait(self.settings.retry_interval_s)
                        continue
                    except Exception:
                        logger.exception("Acquisition failed; retrying")
                        self.set_status("disconnected", "Oscilloscope unavailable or capture failed. Check USB, permissions and service logs; reconnecting automatically.")
                        self.close_source()
                        self.end_session("interrupted")
                        self.stop_event.wait(self.settings.retry_interval_s)
                        continue
                    summary.update(source=self.settings.mode, host_started_ns=started_ns,
                                   host_received_ns=received_ns, host_started_utc=ns_to_utc(started_ns),
                                   host_received_utc=ns_to_utc(received_ns), acquisition_elapsed_s=elapsed,
                                   since_previous_start_s=None if previous_start is None else start - previous_start)
                    try:
                        summary.update(self.weather.associate(received_ns))
                    except Exception:
                        # Weather problems must never stop scope recording.
                        logger.exception("Weather association failed")
                        summary.update(unavailable("missing"))
                    if logging:
                        if self.session_id is None or start - session_started >= self.settings.session_duration_s:
                            self.end_session()
                            self.session_id = self.store.start_session(self.settings, self.weather.metadata())
                            session_started = start
                        saved = self.store.save(self.session_id, stored, summary)
                    else:
                        saved = {**summary, "session_id": None, "capture_id": None}  # Shown live, never saved
                    previous_start = start
                    with self.mutex:
                        self.seq += 1
                        saved["seq"] = self.seq
                        self.latest = {"summary": saved, "preview": preview}
                        self.recent.append(saved)
                        self.last_success = time.monotonic()
                        # The toggle may have changed during this burst; report what was actually done.
                        self.state = "recording" if logging else "monitoring"
                        self.message = self.status_message(logging)
                except Exception:
                    logger.exception("Persistence failed; capture not published")
                    self.set_status("storage_error", "Recording paused: unable to save data. Check disk and service logs.")
                    self.close_source()
                    try:
                        self.end_session("interrupted")
                    except Exception:
                        logger.exception("Unable to mark interrupted recording")
                    self.stop_event.wait(self.settings.retry_interval_s)
                    continue
                self.stop_event.wait(max(0, self.settings.capture_interval_s - (time.monotonic() - start)))
        finally:
            self.close_source()
            try:
                self.end_session()
            except Exception:
                logger.exception("Could not finalise session")
            self.set_status("stopped", "Recorder stopped")
