import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.keys import Keys
from app.main import create_app
from app.provision import provision
from tests.conftest import event


def test_accepts_unauthorized_access_and_reads_back_verified(robot):
    payload = event()
    reply = robot.post(payload)
    assert reply.status_code == 201 and reply.json()["integrity"] == "verified"
    stored = robot.view("/api/v1/events/" + payload["event_id"]).json()
    assert stored["person"]["person_id"] == "Visitor_01" and stored["integrity"] == "verified"
    assert stored["timestamp"].endswith("Z") and stored["robot_id"] == "SG-R1"
    status = robot.view("/api/v1/robots").json()[0]
    assert status["zone"] == "C2" and status["last_event_type"] == "unauthorized_access"


def test_person_evidence_is_encrypted_at_rest(robot):
    payload = event()
    robot.post(payload)
    store = robot.client.app.state.store
    with store.connect() as db:
        record, evidence = db.execute("SELECT record,evidence FROM events").fetchone()
    assert "Visitor_01" not in record and "Visitor_01" not in evidence and "SG-VIS-3001" not in evidence


@pytest.mark.parametrize("changes", [
    dict(event_type="person_detected", severity="info",
         person={"person_id": "Employee_01", "classification": "AUTHORIZED", "zone": "B2", "distance_m": 3.6}, zone="B2"),
    dict(event_type="fire_detected", severity="critical", person=None, message="Simulated fire"),
    dict(event_type="smoke_detected", severity="warning", person=None),
    dict(event_type="patrol_status", severity="info", person=None, zone="B1", message="SG-R1 entered B1"),
    dict(event_type="robot_status", severity="info", person=None, zone="UNMAPPED",
         robot={"speed_mps": 0.5, "heading_rad": 1.2, "camera_enabled": True, "sim_time_s": 12.0}),
])
def test_all_event_types(robot, changes):
    assert robot.post(event(**changes)).status_code == 201


@pytest.mark.parametrize("changes", [
    dict(severity="extreme"), dict(severity="info"), dict(person=None), dict(zone="Z9"),
    dict(position={"x": 99, "y": 0}), dict(position={"x": "1", "y": 0}),
    dict(timestamp="2026-01-01T10:00:00"), dict(timestamp="yesterday"), dict(event_id="bad id!"),
    dict(event_type="teleport"), dict(extra="field"), dict(source="real-camera"),
    dict(person={"person_id": "Visitor_01", "classification": "AUTHORIZED", "zone": "C2", "distance_m": 1}),
    dict(event_type="robot_status", severity="info", person=None),
    dict(event_type="fire_detected", severity="info", person=None),
])
def test_rejects_invalid_fields(robot, changes):
    assert robot.post(event(**changes)).status_code == 422


def test_rejects_nan_and_out_of_window_timestamps(robot):
    assert robot.post(None, raw=b'{"position":{"x":NaN}}').status_code == 422
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert robot.post(event(timestamp=future)).status_code == 422
    assert robot.post(event(timestamp=old)).status_code == 422
    late = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert robot.post(event(timestamp=late)).status_code == 201  # queued while backend was down


def test_robot_authentication_and_replay(robot):
    payload = event()
    assert robot.client.post("/api/v1/events", json=payload).status_code == 401
    assert robot.post(payload, secret=b"x" * 32).status_code == 401
    assert robot.post(payload, robot_id="SG-R9").status_code == 401
    assert robot.post(payload, stamp=int(time.time()) - 600).status_code == 401
    assert robot.post(event(robot_id="SG-R2")).status_code == 403
    nonce = "n" * 24
    assert robot.post(payload, nonce=nonce).status_code == 201
    assert robot.post(event(), nonce=nonce).status_code == 409  # replayed request


def test_https_required(robot):
    plain = TestClient(robot.client.app, base_url="http://testserver")
    assert plain.get("/healthz").status_code == 426


def test_idempotent_retry_and_conflict(robot):
    payload = event()
    assert robot.post(payload).status_code == 201
    assert robot.post(payload).status_code == 200      # bridge retry after lost reply
    assert robot.post({**payload, "severity": "critical"}).status_code == 409
    assert len(robot.view("/api/v1/events").json()) == 1


def test_rate_limit(make_robot):
    robot = make_robot(rate_per_s=0.01, burst=3)
    codes = [robot.post(event()).status_code for _ in range(5)]
    assert codes[:3] == [201] * 3 and codes[3:] == [429, 429]


def test_viewer_token_required(robot):
    assert robot.client.get("/api/v1/events").status_code == 401
    assert robot.client.get("/api/v1/events", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert robot.client.get("/api/v1/audit").status_code == 401
    assert robot.client.get("/").status_code == 200  # page itself holds no data


def test_tampering_detected_on_read_and_audit(robot):
    a, b = event(), event()
    robot.post(a); robot.post(b)
    store = robot.client.app.state.store
    with store.connect() as db, db:
        db.execute("UPDATE events SET record=replace(record,'\"high\"','\"info\"') WHERE event_id=?", (a["event_id"],))
        cipher = db.execute("SELECT evidence FROM events WHERE event_id=?", (b["event_id"],)).fetchone()[0]
        db.execute("UPDATE events SET evidence=? WHERE event_id=?", (cipher[:-6] + "AAAA==", b["event_id"]))
    audit = robot.view("/api/v1/audit").json()
    assert audit["invalid"] == 2 and audit["verified"] == 0 and "not prove" in audit["note"]
    assert robot.view("/api/v1/events/" + a["event_id"]).json()["integrity"] == "invalid"


def test_key_rules(tmp_path):
    with pytest.raises(RuntimeError):
        Keys(b"k" * 32, b"k" * 32)
    folder = tmp_path / "s"
    provision(folder)
    before = {p.name: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    assert provision(folder) == []  # never overwrites
    assert before == {p.name: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    assert "redacted" in repr(Keys.load(folder))


def test_database_is_separate_sqlite(robot, tmp_path):
    robot.post(event())
    tables = {r[0] for r in sqlite3.connect(robot.client.app.state.store.path).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "nonces", "robot_status", "elastic_outbox"} <= tables
