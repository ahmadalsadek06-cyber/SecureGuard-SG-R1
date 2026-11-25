"""SecureGuard SG-R1 event backend: Webots SG-R1 -> this API -> (optional) Elasticsearch."""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from . import auth, elastic
from .keys import Keys
from .models import EventType, RobotEvent
from .store import Conflict, Replay, Store

BACKEND = Path(__file__).resolve().parents[1]
CLOCK_SKEW_S = 120          # request signature freshness window
MAX_EVENT_AGE = timedelta(hours=24)   # queued robot events may arrive late
MAX_BODY = 16 * 1024
logger = logging.getLogger("secureguard")


def create_app(secrets_dir: Path | None = None, db_path: Path | None = None, elastic_config="env",
               rate_per_s: float = 20, burst: int = 40) -> FastAPI:
    secrets_dir = Path(secrets_dir or os.getenv("SG_SECRETS_DIR", BACKEND / "secrets"))
    db_path = Path(db_path or os.getenv("SG_DB", BACKEND / "data" / "secureguard_events.sqlite3"))
    config = elastic.from_environment() if elastic_config == "env" else elastic_config
    export_status = os.getenv("SG_EXPORT_ROBOT_STATUS", "false").lower() == "true"
    keys = Keys.load(secrets_dir)
    robots = auth.RobotCredentials.load(secrets_dir)
    viewer_hash = auth.viewer_token_hash(secrets_dir)
    store = Store(db_path, keys, outbox=config is not None)
    limiter = auth.RateLimiter(rate_per_s, burst)
    wake = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        task = None
        if config is not None:
            app.state.exporter = elastic.Exporter(store, config)
            task = asyncio.create_task(elastic.worker(app.state.exporter, wake))
            logger.warning("Elastic export ENABLED -> %s", elastic.DATA_STREAM)
        yield
        if task:
            task.cancel()

    app = FastAPI(title="SecureGuard SG-R1 Event Backend", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.exporter = None

    @app.middleware("http")
    async def https_only(request: Request, call_next):
        if request.url.scheme != "https":
            return JSONResponse(status_code=426, content={"detail": "HTTPS required."})
        reply = await call_next(request)
        reply.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                              "Strict-Transport-Security": "max-age=31536000",
                              "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                         "style-src 'unsafe-inline'; frame-ancestors 'none'"})
        return reply

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, exc):
        return JSONResponse(status_code=422, content={"detail": [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]})

    def viewer(request: Request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or not auth.token_matches(header[7:], viewer_hash):
            raise HTTPException(401, "Viewer token required.", headers={"WWW-Authenticate": "Bearer"})

    async def robot(request: Request) -> tuple[str, bytes]:
        client = request.client.host if request.client else "?"
        if not limiter.allow(client):
            raise HTTPException(429, "Rate limit exceeded.")
        h = request.headers
        robot_id, stamp, nonce, signature = (h.get("x-sg-robot-id"), h.get("x-sg-timestamp"),
                                             h.get("x-sg-nonce"), h.get("x-sg-signature"))
        if not all((robot_id, stamp, nonce, signature)) or not auth.NONCE.fullmatch(nonce) or not stamp.isdigit():
            raise HTTPException(401, "Signed robot headers required.")
        secret = robots.secret(robot_id)
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "Event too large.")
        expected = auth.sign(secret or b"\0" * 32, request.method, request.url.path, stamp, nonce, body)
        if secret is None or not auth.hmac.compare_digest(expected, signature):
            raise HTTPException(401, "Invalid robot signature.")
        if abs(time.time() - int(stamp)) > CLOCK_SKEW_S:
            raise HTTPException(401, "Request timestamp outside allowed clock skew.")
        try:
            store.use_nonce(robot_id, nonce, 2 * CLOCK_SKEW_S)
        except Replay:
            raise HTTPException(409, "Nonce already used; replay rejected.")
        return robot_id, body

    @app.post("/api/v1/events", status_code=201)
    async def ingest(request: Request, credential=Depends(robot)):
        robot_id, body = credential
        try:
            event = RobotEvent.model_validate_json(body)
        except ValidationError as exc:
            return JSONResponse(status_code=422, content={"detail": [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]})
        if event.robot_id != robot_id:
            raise HTTPException(403, "robot_id does not match the authenticated robot.")
        now = datetime.now(timezone.utc)
        if event.timestamp > now + timedelta(seconds=CLOCK_SKEW_S) or event.timestamp < now - MAX_EVENT_AGE:
            raise HTTPException(422, "Event timestamp outside the accepted window.")
        exportable = export_status or event.event_type != "robot_status"
        try:
            created, view = await asyncio.to_thread(store.add, event, exportable)
        except Conflict:
            raise HTTPException(409, "Event ID already stored with different content.")
        if created and config is not None and exportable:
            wake.set()
        return JSONResponse(status_code=201 if created else 200,
                            content={"event_id": event.event_id, "stored": created, "integrity": view["integrity"]})

    @app.get("/api/v1/events", dependencies=[Depends(viewer)])
    def events(limit: int = Query(100, ge=1, le=500), event_type: EventType | None = None):
        return store.list(limit, event_type)

    @app.get("/api/v1/events/{event_id}", dependencies=[Depends(viewer)])
    def one(event_id: str):
        view = store.get(event_id)
        if view is None:
            raise HTTPException(404, "Not found.")
        return view

    @app.get("/api/v1/robots", dependencies=[Depends(viewer)])
    def robots_status():
        return store.robots()

    @app.get("/api/v1/audit", dependencies=[Depends(viewer)])
    def audit():
        result = store.audit()
        result["note"] = ("Backend HMAC shows records are unchanged since this backend accepted them. "
                          "It does not prove simulated camera observations are physically authentic.")
        result["elastic"] = app.state.exporter.status() if app.state.exporter else "disabled"
        return result

    @app.get("/healthz")
    def health():
        return {"status": "ok", "elastic_export": "enabled" if config is not None else "disabled"}

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return (BACKEND / "app" / "dashboard.html").read_text(encoding="utf-8")

    return app
