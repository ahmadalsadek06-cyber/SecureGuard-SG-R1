"""SQLite event store (separate database). Person identity/message evidence is AES-GCM sealed;
every stored record carries a backend HMAC tag that is verified on read and audit."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag

from .keys import Keys, canonical
from .models import RobotEvent

SENSITIVE = ("person", "message")


class Conflict(Exception):
    pass


class Replay(Exception):
    pass


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Store:
    def __init__(self, path: Path, keys: Keys, outbox: bool = False):
        self.path, self.keys, self.outbox = Path(path), keys, outbox

    def connect(self):
        return closing(sqlite3.connect(self.path, timeout=5))

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, robot_id TEXT NOT NULL, event_type TEXT NOT NULL,
                severity TEXT NOT NULL, zone TEXT NOT NULL, ts TEXT NOT NULL, received_at TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, record TEXT NOT NULL, evidence TEXT, tag TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts DESC);
            CREATE TABLE IF NOT EXISTS nonces(robot_id TEXT NOT NULL, nonce TEXT NOT NULL, expires REAL NOT NULL,
                PRIMARY KEY(robot_id, nonce));
            CREATE TABLE IF NOT EXISTS robot_status(robot_id TEXT PRIMARY KEY, last_seen TEXT NOT NULL,
                last_event_id TEXT NOT NULL, last_event_type TEXT NOT NULL, zone TEXT NOT NULL,
                x REAL NOT NULL, y REAL NOT NULL, state TEXT);
            CREATE TABLE IF NOT EXISTS elastic_outbox(event_id TEXT PRIMARY KEY, queued_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                last_status TEXT, done_at TEXT);
            CREATE INDEX IF NOT EXISTS outbox_due ON elastic_outbox(state, next_attempt);
            """)

    def use_nonce(self, robot_id: str, nonce: str, ttl_s: float):
        now = time.time()
        with self.connect() as db, db:
            db.execute("DELETE FROM nonces WHERE expires < ?", (now,))
            try:
                db.execute("INSERT INTO nonces VALUES(?,?,?)", (robot_id, nonce, now + ttl_s))
            except sqlite3.IntegrityError as exc:
                raise Replay() from exc

    def add(self, event: RobotEvent, exportable: bool):
        """Return (created, stored_view). Same ID + same content is idempotent; different content conflicts."""
        body = event.model_dump(mode="json")
        digest = hashlib.sha256(canonical(body)).hexdigest()
        received = utc_iso(datetime.now(timezone.utc))
        record = {k: v for k, v in body.items() if k not in SENSITIVE}
        record["timestamp"] = utc_iso(event.timestamp)
        record["received_at"] = received
        evidence_fields = {k: body[k] for k in SENSITIVE if body.get(k) is not None}
        evidence = self.keys.seal(evidence_fields, "event/" + event.event_id) if evidence_fields else None
        tag = self.keys.tag({"record": record, "evidence": evidence})
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT content_sha256 FROM events WHERE event_id=?", (event.event_id,)).fetchone()
            if row:
                if row[0] != digest:
                    raise Conflict(event.event_id)
                return False, self._view(*db.execute(
                    "SELECT record,evidence,tag FROM events WHERE event_id=?", (event.event_id,)).fetchone())
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (event.event_id, event.robot_id, event.event_type, event.severity, event.zone,
                        record["timestamp"], received, digest, json.dumps(record), evidence, tag))
            db.execute("""INSERT INTO robot_status VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(robot_id) DO UPDATE SET last_seen=excluded.last_seen, last_event_id=excluded.last_event_id,
                last_event_type=excluded.last_event_type, zone=excluded.zone, x=excluded.x, y=excluded.y,
                state=COALESCE(excluded.state, robot_status.state)""",
                       (event.robot_id, received, event.event_id, event.event_type, event.zone,
                        event.position.x, event.position.y, json.dumps(body["robot"]) if event.robot else None))
            if self.outbox and exportable:
                db.execute("INSERT OR IGNORE INTO elastic_outbox(event_id,queued_at,next_attempt) VALUES(?,?,?)",
                           (event.event_id, received, received))
        return True, self._view(json.dumps(record), evidence, tag)

    def _view(self, record_json, evidence, tag):
        """Decrypt evidence and verify the HMAC tag. Never raises on tampering; reports it."""
        record = json.loads(record_json)
        view = dict(record)
        integrity = "verified" if self.keys.verify({"record": record, "evidence": evidence}, tag) else "invalid"
        if evidence:
            try:
                view.update(self.keys.open(evidence, "event/" + record["event_id"]))
            except (InvalidTag, ValueError):
                integrity = "invalid"
        view["integrity"] = integrity
        return view

    def get(self, event_id):
        with self.connect() as db:
            row = db.execute("SELECT record,evidence,tag FROM events WHERE event_id=?", (event_id,)).fetchone()
        return self._view(*row) if row else None

    def list(self, limit=100, event_type=None):
        sql, args = "SELECT record,evidence,tag FROM events", []
        if event_type:
            sql += " WHERE event_type=?"; args.append(event_type)
        sql += " ORDER BY ts DESC, event_id DESC LIMIT ?"; args.append(limit)
        with self.connect() as db:
            return [self._view(*row) for row in db.execute(sql, args)]

    def audit(self):
        counts = {"verified": 0, "invalid": 0}
        invalid = []
        with self.connect() as db:
            for row in db.execute("SELECT record,evidence,tag FROM events"):
                view = self._view(*row)
                counts[view["integrity"]] += 1
                if view["integrity"] != "verified":
                    invalid.append(view.get("event_id"))
        return {**counts, "invalid_event_ids": invalid[:100]}

    def robots(self):
        with self.connect() as db:
            rows = db.execute("SELECT robot_id,last_seen,last_event_id,last_event_type,zone,x,y,state FROM robot_status").fetchall()
        return [{"robot_id": r[0], "last_seen": r[1], "last_event_id": r[2], "last_event_type": r[3], "zone": r[4],
                 "position": {"x": r[5], "y": r[6]}, "state": json.loads(r[7]) if r[7] else None} for r in rows]
