from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import json
import logging
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .service import Recorder
from .storage import PLOT_FIELDS, SessionActive, utc_to_ns

logger = logging.getLogger(__name__)


NPZ_LIMIT_BYTES = 1_500_000_000  # session.npz is assembled in memory
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")
CONTROL_HEADER = "x-oclab-control"


class LoggingRequest(BaseModel):
    enabled: bool


def control_access(request: Request, token: str) -> tuple[bool, str]:
    """Whether this request may switch logging, and why not.

    Allowed directly from this PC (loopback, no proxy headers: Caddy always adds
    them), or anywhere with the configured control_token. Browsers cannot send the
    custom header cross-site without a CORS preflight, which this app never grants.
    """
    supplied = request.headers.get("x-control-token", "")
    if token and supplied and secrets.compare_digest(supplied.encode(), token.encode()):
        return True, ""
    if supplied:
        return False, "Control token not accepted"
    try:
        loopback = ipaddress.ip_address(request.client.host).is_loopback
    except (AttributeError, ValueError):
        loopback = False
    if loopback and not any(h in request.headers for h in PROXY_HEADERS):
        return True, ""
    return False, "Logging can be switched from a browser on the acquisition PC" + (", or with the control token" if token else "")


class ManagedStream(StreamingResponse):
    """Always return a capacity slot, including early disconnects and send errors."""

    def __init__(self, *args, release, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.release()


def create_app(settings: Settings | None = None, recorder: Recorder | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.validate()
    recorder = recorder or Recorder(settings)
    exports = threading.BoundedSemaphore(2)
    streams = asyncio.Semaphore(100)

    @asynccontextmanager
    async def lifespan(app):
        recorder.start()
        try:
            yield
        finally:
            await asyncio.to_thread(recorder.stop)

    app = FastAPI(title="OC Lab live-link research API", version="0.1.0", lifespan=lifespan)
    app.state.recorder = recorder
    static = Path(__file__).parent / "static"

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    @app.get("/api/config")
    def configuration():
        return settings.public()

    @app.get("/api/live")
    def live():
        return recorder.snapshot()

    @app.get("/api/health")
    def health():
        snapshot = recorder.snapshot()
        state = snapshot["status"]
        # The status code reflects fresh scope data only (logging on or off); weather and
        # the logging state are reported alongside.
        w = snapshot["weather"]
        weather = {k: w[k] for k in ("enabled", "state", "message", "stale", "age_s", "port")}
        return JSONResponse({**state, "weather": weather}, status_code=200 if state["acquiring"] else 503)

    @app.get("/api/control")
    def control(request: Request):
        allowed, reason = control_access(request, settings.control_token)
        return {"allowed": allowed, "reason": reason, "token_available": bool(settings.control_token),
                "logging": recorder.snapshot()["status"]["logging"]}

    def require_control(request: Request):
        """Same rules for every change: custom header, same-site origin, this PC or the control token."""
        if request.headers.get(CONTROL_HEADER) != "1":
            raise HTTPException(403, "Missing control header")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "Cross-site control request refused")
        allowed, reason = control_access(request, settings.control_token)
        if not allowed:
            raise HTTPException(403, reason)

    @app.post("/api/logging")
    def set_logging(body: LoggingRequest, request: Request):
        require_control(request)
        recorder.set_logging(body.enabled)
        return recorder.snapshot()["status"]

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str, request: Request):
        require_control(request)
        try:
            deleted = recorder.store.delete_session(session_id, active_id=recorder.snapshot()["status"]["session_id"])
        except SessionActive:
            raise HTTPException(409, "This session is still recording; stop logging first")
        if deleted is None:
            raise HTTPException(404, "Recording session not found")
        logger.info("Deleted session %s (%d captures)", session_id, deleted)
        return {"deleted": session_id, "captures": deleted}

    @app.get("/api/plot-fields")
    def plot_fields():
        return {name: {"label": label, "unit": unit} for name, (label, unit) in PLOT_FIELDS.items()}

    @app.get("/api/sessions/{session_id}/series")
    def series(session_id: str, fields: str = Query(..., max_length=500), points: int = Query(2000, ge=10, le=5000)):
        session = get_session(session_id)
        names = [f for f in fields.split(",") if f]
        unknown = [f for f in names if f not in PLOT_FIELDS]
        if not names or len(names) > 6 or unknown:
            raise HTTPException(422, f"Choose 1 to 6 of: {', '.join(PLOT_FIELDS)}" + (f" (unknown: {', '.join(unknown)})" if unknown else ""))
        calibration = session["metadata"]["settings"]["calibration"]
        position_unit = "mm" if calibration.get("x_scale_mm") is not None else \
            "ratio" if calibration.get("position_model", "difference_over_sum") == "difference_over_sum" else "V"
        data = recorder.store.series(session_id, names, points)
        units = {f: PLOT_FIELDS[f][1] if PLOT_FIELDS[f][1] is not None else position_unit for f in names}
        return {"session_id": session_id, "mode": session["mode"], "units": units, **data}

    @app.get("/api/history")
    def history(limit: int = Query(600, ge=1, le=7200)):
        return {"items": recorder.store.history(limit)}

    @app.get("/api/weather/live")
    def weather_live():
        return recorder.weather.snapshot()

    @app.get("/api/weather/history")
    def weather_history(seconds: int = Query(300, ge=1, le=3600)):
        since_ns = time.time_ns() - seconds * 1_000_000_000
        return {"seconds": seconds, "source": settings.mode, **recorder.store.weather_history(settings.mode, since_ns)}

    @app.get("/api/events")
    async def events(request: Request):
        if streams.locked():
            raise HTTPException(503, "Live connection limit reached")
        await streams.acquire()
        async def stream():
            last_seq = 0
            while not await request.is_disconnected():
                snapshot = recorder.snapshot()
                snapshot["captures"] = recorder.captures_since(last_seq)
                if snapshot["captures"]:
                    last_seq = snapshot["captures"][-1]["seq"]
                yield "event: snapshot\ndata: " + json.dumps(snapshot, allow_nan=False) + "\n\n"
                await asyncio.sleep(1)
        return ManagedStream(stream(), release=streams.release, media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    @app.get("/api/sessions")
    def sessions(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        return recorder.store.sessions(limit, offset)

    def get_session(session_id):
        session = recorder.store.session(session_id)
        if session is None:
            raise HTTPException(404, "Recording session not found")
        return session

    @app.get("/api/sessions/{session_id}/metadata.json")
    def metadata(session_id: str):
        session = get_session(session_id)
        return JSONResponse(session, headers={"Content-Disposition": f'attachment; filename="{session["id"]}-metadata.json"'})

    def download(session_id, raw):
        session = get_session(session_id)
        if not exports.acquire(blocking=False):
            raise HTTPException(429, "Two exports are already running; retry shortly", headers={"Retry-After": "10"})
        suffix = "raw.csv.gz" if raw else "summary.csv"
        generator = recorder.store.raw_csv_gzip(session) if raw else recorder.store.summary_csv(session)
        return ManagedStream(generator, release=exports.release, media_type="application/gzip" if raw else "text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{session["id"]}-{suffix}"',
                                          "X-Export-Through-Capture-ID": str(session["export_through_capture_id"])})

    @app.get("/api/sessions/{session_id}/weather.csv")
    def weather_csv(session_id: str):
        session = get_session(session_id)
        # Include the association window before the session's first capture.
        association = (session["metadata"].get("weather") or {}).get("association") or {}
        window_s = association.get("window_s", settings.weather.association_window_s)
        from_ns = utc_to_ns(session["started_utc"]) - round(window_s * 1e9)
        through_ns = utc_to_ns(session["ended_utc"]) if session["ended_utc"] else time.time_ns()
        with recorder.weather.mutex:
            committed = recorder.weather.committed_through_ns
        if committed is not None:
            through_ns = min(through_ns, committed)  # Rows still waiting for the next flush are excluded.
        if not exports.acquire(blocking=False):
            raise HTTPException(429, "Two exports are already running; retry shortly", headers={"Retry-After": "10"})
        return ManagedStream(recorder.store.weather_csv(session["mode"], from_ns, through_ns), release=exports.release,
                             media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{session["id"]}-weather.csv"',
                                      "X-Export-Weather-From-NS": str(from_ns), "X-Export-Weather-Through-NS": str(through_ns)})

    def stream_export(session, generator, suffix, media_type):
        if not exports.acquire(blocking=False):
            raise HTTPException(429, "Two exports are already running; retry shortly", headers={"Retry-After": "10"})
        return ManagedStream(generator, release=exports.release, media_type=media_type,
                             headers={"Content-Disposition": f'attachment; filename="{session["id"]}-{suffix}"',
                                      "X-Export-Through-Capture-ID": str(session["export_through_capture_id"])})

    @app.get("/api/sessions/{session_id}/minutes.csv")
    def minutes_csv(session_id: str):
        session = get_session(session_id)
        return stream_export(session, recorder.store.minutes_csv(session), "minutes.csv", "text/csv")

    @app.get("/api/sessions/{session_id}/session.npz")
    def session_npz(session_id: str):
        session = get_session(session_id)
        if not recorder.store.decimated(session):
            raise HTTPException(409, "Binary export is available for sessions recorded with schema 3 or later; use raw.csv.gz")
        if recorder.store.npz_size(session) > NPZ_LIMIT_BYTES:
            raise HTTPException(413, "Session too large for a single .npz; shorten session_duration_s or use raw.csv.gz")
        return stream_export(session, recorder.store.session_npz(session), "session.npz", "application/octet-stream")

    @app.get("/api/sessions/{session_id}/summary.csv")
    def summary_csv(session_id: str):
        return download(session_id, False)

    @app.get("/api/sessions/{session_id}/raw.csv.gz")
    def raw_csv(session_id: str):
        return download(session_id, True)

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app
