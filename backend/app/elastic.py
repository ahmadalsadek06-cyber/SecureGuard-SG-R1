"""Optional Elasticsearch exporter (disabled unless configured).

Enabled only when BOTH ELASTICSEARCH_URL (https) and ELASTICSEARCH_API_KEY are set in the
backend process environment. The key is read at startup, never logged, stored or returned.

Transactional outbox: the event ID is queued in the same SQLite transaction that stores the
event. The worker re-verifies the record's HMAC tag, then PUTs one ECS document to
logs-secureguard.events-default/_create/<event_id>: 201 = exported, 409 = already exported
(no duplicates). 408/429/5xx/network errors retry with exponential backoff; 400/401/403 stay
pending and retry every 15 minutes. Local records are never deleted.
"""
import asyncio
import json
import logging
import os
import sqlite3
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DATA_STREAM = "logs-secureguard.events-default"
ECS_VERSION = "8.11.0"
SEVERITY = {"info": 21, "warning": 47, "high": 73, "critical": 99}
TRANSIENT = {408, 429, 500, 502, 503, 504}
MAX_BACKOFF_S = 900
BATCH = 50
logger = logging.getLogger("secureguard.elastic")


class Config:
    def __init__(self, url, api_key):
        self.url, self._api_key = url.rstrip("/"), api_key

    def __repr__(self):
        return f"Config(url={self.url!r}, api_key=<redacted>)"

    @property
    def authorization(self):
        return "ApiKey " + self._api_key


def from_environment(env=None):
    env = os.environ if env is None else env
    url = env.get("ELASTICSEARCH_URL", "").strip()
    key = env.get("ELASTICSEARCH_API_KEY", "").strip()
    if not url or not key:
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query:
        raise RuntimeError("ELASTICSEARCH_URL must be a plain https:// endpoint without credentials.")
    return Config(url, key)


def iso(value: datetime):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def document(view: dict) -> dict:
    """ECS-compatible document for one verified record. All events are simulated."""
    etype, person = view["event_type"], view.get("person")
    security = etype in ("unauthorized_access", "person_detected")
    hazard = etype in ("fire_detected", "smoke_detected")
    doc = {
        "@timestamp": view["timestamp"],
        "ecs": {"version": ECS_VERSION},
        "data_stream": {"type": "logs", "dataset": "secureguard.events", "namespace": "default"},
        "message": view.get("message") or f"SG-R1 {etype} in zone {view['zone']} (simulated)",
        "event": {"id": view["event_id"], "action": etype, "dataset": "secureguard.events", "module": "secureguard",
                  "kind": "alert" if view["severity"] in ("high", "critical") else ("state" if etype.endswith("_status") else "event"),
                  "category": ["intrusion_detection"] if security else (["threat"] if hazard else ["host"]),
                  "type": ["denied"] if etype == "unauthorized_access" else ["info"],
                  "severity": SEVERITY[view["severity"]], "created": view["received_at"],
                  "provider": "webots-sgr1-simulation"},
        "log": {"level": view["severity"]},
        "observer": {"name": view["robot_id"], "type": "robot", "vendor": "SecureGuard", "product": "SG-R1 (Webots simulation)"},
        "labels": {"simulated": "true", "zone": view["zone"]},
        "secureguard": {"robot_id": view["robot_id"], "zone": view["zone"], "severity": view["severity"],
                        "position": view["position"], "simulated": True, "source": view["source"],
                        "integrity": "backend_hmac_verified"},
    }
    if person:
        doc["user"] = {"id": person["person_id"], "roles": [person["role"]] if person.get("role") else []}
        doc["secureguard"]["person"] = {k: person.get(k) for k in ("person_id", "classification", "badge_id", "zone", "distance_m")}
    if view.get("robot"):
        doc["secureguard"]["robot"] = view["robot"]
    return doc


def send(config, event_id, doc, timeout=10):
    """PUT _create/<id>. Returns HTTP status, or 0 for a network failure."""
    url = f"{config.url}/{DATA_STREAM}/_create/{urllib.parse.quote(event_id, safe='')}"
    request = urllib.request.Request(url, data=json.dumps(doc).encode(), method="PUT",
                                     headers={"Authorization": config.authorization, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as reply:
            return reply.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


class Exporter:
    def __init__(self, store, config, transport=None, clock=lambda: datetime.now(timezone.utc)):
        self.store, self.config, self.transport, self.clock = store, config, transport, clock

    def run_once(self):
        counts = {"exported": 0, "duplicate": 0, "retry": 0, "skipped": 0}
        now = self.clock()
        with self.store.connect() as db:
            due = [r[0] for r in db.execute("SELECT event_id FROM elastic_outbox WHERE state='pending' AND next_attempt<=? "
                                             "ORDER BY next_attempt LIMIT ?", (iso(now), BATCH))]
        for event_id in due:
            view = self.store.get(event_id)
            if view is None or view["integrity"] != "verified":
                self._set(event_id, "skipped", "integrity"); counts["skipped"] += 1
                continue
            status = (self.transport or send)(self.config, event_id, document(view))
            if status in (200, 201):
                self._set(event_id, "exported", str(status)); counts["exported"] += 1
            elif status == 409:
                self._set(event_id, "exported", "409 duplicate"); counts["duplicate"] += 1
            else:
                self._retry(event_id, status, now); counts["retry"] += 1
        return counts

    def _set(self, event_id, state, status):
        with self.store.connect() as db, db:
            db.execute("UPDATE elastic_outbox SET state=?,last_status=?,done_at=?,attempts=attempts+1 WHERE event_id=?",
                       (state, status, iso(self.clock()), event_id))

    def _retry(self, event_id, status, now):
        with self.store.connect() as db, db:
            attempts = db.execute("SELECT attempts FROM elastic_outbox WHERE event_id=?", (event_id,)).fetchone()[0] + 1
            transient = status in TRANSIENT or status == 0
            delay = min(5 * 2 ** min(attempts, 10), MAX_BACKOFF_S) if transient else MAX_BACKOFF_S
            db.execute("UPDATE elastic_outbox SET attempts=?,next_attempt=?,last_status=? WHERE event_id=?",
                       (attempts, iso(now + timedelta(seconds=delay)), str(status or "network"), event_id))
        logger.warning("Elastic export of %s deferred (status %s, attempt %d)", event_id, status or "network", attempts)

    def status(self):
        with self.store.connect() as db:
            return dict(db.execute("SELECT state,COUNT(*) FROM elastic_outbox GROUP BY state").fetchall())


async def worker(exporter, wake: asyncio.Event, interval=15):
    while True:
        try:
            await asyncio.to_thread(exporter.run_once)
        except sqlite3.Error as exc:
            logger.error("Elastic outbox unavailable: %s", exc)
        try:
            await asyncio.wait_for(wake.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        wake.clear()
