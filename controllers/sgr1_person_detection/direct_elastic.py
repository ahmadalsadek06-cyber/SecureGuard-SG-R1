"""Direct SG-R1 -> Elasticsearch exporter (optional mode, Python stdlib only).

Select with SG_EVENT_DESTINATION=elastic (default: backend). Same interface as
BackendBridge (submit / close / from_environment), so the controller's driving,
recognition, authorization and cooldown logic are unchanged.

Security model
- URL and API key come ONLY from ELASTICSEARCH_URL / ELASTICSEARCH_API_KEY in the Webots
  process environment. The key is never printed, logged, written to the spool or repr().
- HTTPS only, certificate + hostname verification always on (ssl.create_default_context).
  SG_ELASTIC_CA_FILE may add a private CA (tests); verification is never disabled.
- Use a dedicated API key limited to `create_doc` on logs-secureguard.events-default.

Delivery
- submit() never blocks: bounded in-memory queue -> one daemon worker thread.
- Each event is written to a small durable spool (one JSON file per event, no credentials)
  before sending and deleted only after Elasticsearch confirms it. Spooled events are
  re-sent after a restart.
- PUT /logs-secureguard.events-default/_create/<event_id>: the stable event ID makes retries
  idempotent. 201 = created, 409 = already present (duplicate, treated as delivered).
- Network errors, 408, 429, 5xx: exponential backoff (1 s -> 60 s), same event retried.
- 401/403: credentials/permissions problem, kept in spool, retried every 60 s, reported once.
- Other 4xx (e.g. 400 mapping error): moved to spool/rejected/ and reported (never silent).
- Queue overflow: event spooled to disk and reported; only if the spool limit is also
  reached is an event dropped, and that is counted and reported.

Trade-off (see DIRECT_ELASTIC_README.md): this path has NO backend request HMAC,
NO SQLite storage/AES-GCM evidence encryption, NO local integrity audit and NO dashboard.
"""
import importlib.util
import json
import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
DATA_STREAM = "logs-secureguard.events-default"
ALLOWED_TYPES = {"unauthorized_access", "person_detected", "fire_detected", "smoke_detected",
                 "patrol_status", "robot_status"}
ZONES = {"A1", "A2", "B1", "B2", "C1", "C2", "UNMAPPED"}
SEVERITIES = {"info", "warning", "high", "critical"}
TRANSIENT = {408, 429, 500, 502, 503, 504}
MAX_QUEUE = 500
MAX_SPOOL = 5000
MAX_BACKOFF_S = 60.0


def _load_ecs_document():
    """Reuse the backend's ECS mapping so both architectures produce identical fields."""
    path = PROJECT / "backend" / "app" / "elastic.py"
    spec = importlib.util.spec_from_file_location("sg_backend_elastic_mapping", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)          # stdlib-only module: no FastAPI import
    return module.document


ecs_document = _load_ecs_document()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_document(event):
    """Event dict from the controller -> ECS document (same fields as the backend exporter)."""
    view = dict(event)
    view.setdefault("received_at", event.get("created_at") or event["timestamp"])
    doc = ecs_document(view)
    doc["secureguard"]["integrity"] = "not_verified_direct_mode"   # no backend HMAC on this path
    doc["secureguard"]["transport"] = "direct_robot_to_elastic"
    return doc


def validate(event):
    """Light local checks (the backend's Pydantic validation is not present in direct mode)."""
    problems = []
    if event.get("event_type") not in ALLOWED_TYPES: problems.append("event_type")
    if event.get("zone") not in ZONES: problems.append("zone")
    if event.get("severity") not in SEVERITIES: problems.append("severity")
    pos = event.get("position") or {}
    if not (isinstance(pos.get("x"), (int, float)) and isinstance(pos.get("y"), (int, float))): problems.append("position")
    if event.get("event_type") in ("unauthorized_access", "person_detected") and not event.get("person"):
        problems.append("person")
    return problems


class Config:
    def __init__(self, url, api_key, ca_file=None):
        self.url, self._api_key, self.ca_file = url.rstrip("/"), api_key, ca_file

    def __repr__(self):
        return f"Config(url={self.url!r}, api_key=<redacted>)"

    @property
    def authorization(self):
        return "ApiKey " + self._api_key

    @classmethod
    def from_env(cls, env):
        url = env.get("ELASTICSEARCH_URL", "").strip()
        key = env.get("ELASTICSEARCH_API_KEY", "").strip()
        if not url or not key:
            return None, "ELASTICSEARCH_URL and ELASTICSEARCH_API_KEY must both be set"
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query:
            return None, "ELASTICSEARCH_URL must be a plain https:// endpoint without credentials"
        return cls(url, key, env.get("SG_ELASTIC_CA_FILE") or None), None


def tls_context(ca_file=None):
    context = ssl.create_default_context()              # CERT_REQUIRED + check_hostname
    if ca_file:
        context.load_verify_locations(cafile=ca_file)   # adds a CA; verification stays on
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    return context


class DirectElasticExporter:
    def __init__(self, config, robot_id="SG-R1", spool_dir=None, log=print, opener=None,
                 export_status=False, sleep=time.sleep, max_queue=MAX_QUEUE, max_spool=MAX_SPOOL):
        self.config, self.robot_id, self.log = config, robot_id, log
        self.context = tls_context(config.ca_file)
        self.opener = opener or urllib.request.urlopen
        self.export_status, self.sleep = export_status, sleep
        self.spool = Path(spool_dir or os.getenv("SG_DIRECT_SPOOL_DIR", PROJECT / "data" / "direct_elastic_spool"))
        self.rejected = self.spool / "rejected"
        self.rejected.mkdir(parents=True, exist_ok=True)
        self.max_spool = max_spool
        self.queue = queue.Queue(maxsize=max_queue)
        self.lock = threading.Lock()
        self.stats = {"created": 0, "duplicate": 0, "retries": 0, "rejected": 0, "spooled_on_overflow": 0,
                      "dropped": 0, "skipped_status": 0, "invalid": 0}
        self.running = True
        self.idle = threading.Event(); self.idle.set()
        recovered = self._recover()
        if recovered:
            self.log(f"SG-R1 direct Elastic: {recovered} pending event(s) recovered from spool")
        self.thread = threading.Thread(target=self._run, name="sg-direct-elastic", daemon=True)
        self.thread.start()

    def __repr__(self):
        return f"DirectElasticExporter({self.config!r})"

    @classmethod
    def from_environment(cls, log=print, env=None):
        env = os.environ if env is None else env
        config, error = Config.from_env(env)
        if config is None:
            log(f"SG-R1 direct Elastic: disabled ({error})")
            return None
        exporter = cls(config, log=log, export_status=env.get("SG_EXPORT_ROBOT_STATUS", "false").lower() == "true",
                       spool_dir=env.get("SG_DIRECT_SPOOL_DIR") or None)
        log(f"SG-R1 direct Elastic: enabled -> {config.url}/{DATA_STREAM} (TLS verified)")
        return exporter

    # ---------------------------------------------------------------- producer side (control loop)
    def submit(self, event: dict):
        """Never blocks the robot. Returns the event_id, or None if the event was not accepted."""
        event.setdefault("event_id", "SGR1-" + uuid.uuid4().hex)
        event.setdefault("timestamp", utc_now())
        event.setdefault("robot_id", self.robot_id)
        event.setdefault("source", "webots-sgr1-simulation")
        event.setdefault("created_at", utc_now())
        if event["event_type"] == "robot_status" and not self.export_status:
            self.stats["skipped_status"] += 1            # same default as the backend exporter
            return None
        problems = validate(event)
        if problems:
            self.stats["invalid"] += 1
            self.log(f"SG-R1 direct Elastic: invalid {event.get('event_type')} {event['event_id']} ({', '.join(problems)}) not sent")
            return None
        self.idle.clear()
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            if self._spool_count() < self.max_spool:
                self._write_spool(event)                 # rare: disk I/O only on overflow
                self.stats["spooled_on_overflow"] += 1
                self.log(f"SG-R1 direct Elastic: queue full, {event['event_id']} spooled to disk for later delivery")
            else:
                self.stats["dropped"] += 1
                self.log(f"SG-R1 direct Elastic: queue AND spool full, event DROPPED ({self.stats['dropped']} so far)")
                return None
        return event["event_id"]

    def close(self, timeout=2.0):
        """Try to deliver for up to `timeout` s; anything left stays in the durable spool."""
        self.idle.wait(timeout)
        self.running = False
        pending = self._spool_count()
        if pending:
            self.log(f"SG-R1 direct Elastic: {pending} event(s) left in spool; they will be sent on next start")

    # ---------------------------------------------------------------- spool
    def _spool_path(self, event_id):
        return self.spool / f"{event_id}.json"

    def _write_spool(self, event):
        path = self._spool_path(event["event_id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(event, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)                          # atomic on the same volume

    def _spool_count(self):
        return sum(1 for _ in self.spool.glob("*.json"))

    def _recover(self):
        files = sorted(self.spool.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for path in files:
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
                self.queue.put_nowait(event)
            except (ValueError, OSError):
                os.replace(path, self.rejected / path.name)
            except queue.Full:
                break                                  # rest stays on disk, picked up by _refill
        return len(files)

    def _refill(self):
        if self.queue.empty():
            for path in sorted(self.spool.glob("*.json"), key=lambda p: p.stat().st_mtime)[:50]:
                try:
                    self.queue.put_nowait(json.loads(path.read_text(encoding="utf-8")))
                except (ValueError, OSError, queue.Full):
                    break

    # ---------------------------------------------------------------- network side (worker thread)
    def _put(self, event):
        body = json.dumps(to_document(event), separators=(",", ":")).encode()
        url = f"{self.config.url}/{DATA_STREAM}/_create/{urllib.parse.quote(event['event_id'], safe='')}"
        request = urllib.request.Request(url, data=body, method="PUT", headers={
            "Authorization": self.config.authorization, "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=10, context=self.context) as reply:
                return reply.status
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0

    def _run(self):
        backoff, warned_net, warned_auth = 1.0, False, False
        while self.running:
            try:
                event = self.queue.get(timeout=0.5)
            except queue.Empty:
                self._refill()
                if self.queue.empty():
                    self.idle.set()
                continue
            path = self._spool_path(event["event_id"])
            if not path.exists():
                self._write_spool(event)               # durable before the first attempt
            while self.running:
                status = self._put(event)
                if status in (200, 201, 409):
                    path.unlink(missing_ok=True)
                    self.stats["created" if status != 409 else "duplicate"] += 1
                    self.log(f"SG-R1 direct Elastic: {'created' if status != 409 else 'already present'} "
                             f"{event['event_type']} {event['event_id']} (HTTP {status})")
                    backoff, warned_net, warned_auth = 1.0, False, False
                    break
                if status in TRANSIENT or status == 0:
                    self.stats["retries"] += 1
                    if not warned_net:
                        self.log(f"SG-R1 direct Elastic: temporary failure (HTTP {status or 'network'}); retrying with backoff")
                        warned_net = True
                    self.sleep(backoff); backoff = min(backoff * 2, MAX_BACKOFF_S)
                    continue
                if status in (401, 403):
                    self.stats["retries"] += 1
                    if not warned_auth:
                        self.log(f"SG-R1 direct Elastic: HTTP {status} - check the API key and its create_doc privilege; "
                                 "events stay in the spool")
                        warned_auth = True
                    self.sleep(MAX_BACKOFF_S)
                    continue
                self.stats["rejected"] += 1             # e.g. 400 mapping error: keep evidence, report
                if path.exists():
                    os.replace(path, self.rejected / path.name)
                self.log(f"SG-R1 direct Elastic: {event['event_type']} {event['event_id']} rejected (HTTP {status}); "
                         f"saved in {self.rejected.name}/")
                break


def select_event_sink(log=print, env=None):
    """SG_EVENT_DESTINATION=backend (default) | elastic. Unknown values fall back to backend."""
    env = os.environ if env is None else env
    destination = env.get("SG_EVENT_DESTINATION", "backend").strip().lower() or "backend"
    if destination == "elastic":
        return DirectElasticExporter.from_environment(log, env)
    if destination != "backend":
        log(f"SG-R1: unknown SG_EVENT_DESTINATION={destination!r}; using backend")
    from backend_bridge import BackendBridge
    return BackendBridge.from_environment(log)
