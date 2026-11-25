"""Elastic exporter with mocked responses only; never touches real Elastic data."""
import io
import json
import logging
import urllib.error
from datetime import timedelta

import pytest

from app import elastic
from tests.conftest import event

KEY = "fake-test-key-do-not-use-0123456789"


@pytest.fixture
def exporting(make_robot, monkeypatch):
    sent = []

    def fake(config, event_id, doc):
        sent.append((config.authorization, event_id, doc))
        return fake.replies.pop(0) if fake.replies else 201
    fake.replies = []

    async def idle(*_a, **_k):
        return None
    monkeypatch.setattr(elastic, "worker", idle)  # tests drive run_once deterministically
    robot = make_robot(elastic_config=elastic.Config("https://elastic.invalid", KEY))
    robot.client.app.state.exporter.transport = fake
    return robot, sent, fake


def outbox(robot):
    with robot.client.app.state.store.connect() as db:
        return db.execute("SELECT event_id,state,attempts,last_status FROM elastic_outbox").fetchall()


def test_disabled_until_configured(robot, monkeypatch):
    assert elastic.from_environment({}) is None
    assert elastic.from_environment({"ELASTICSEARCH_URL": "https://x"}) is None
    with pytest.raises(RuntimeError):
        elastic.from_environment({"ELASTICSEARCH_URL": "http://x", "ELASTICSEARCH_API_KEY": KEY})
    assert KEY not in repr(elastic.from_environment({"ELASTICSEARCH_URL": "https://x", "ELASTICSEARCH_API_KEY": KEY}))
    robot.post(event())
    assert outbox(robot) == [] and robot.client.app.state.exporter is None
    assert robot.client.get("/healthz").json()["elastic_export"] == "disabled"


def test_exports_once_with_ecs_fields(exporting, caplog):
    robot, sent, _ = exporting
    caplog.set_level(logging.DEBUG)
    payload = event()
    robot.post(payload)
    robot.post(payload)  # idempotent retry is not queued twice
    exporter = robot.client.app.state.exporter
    assert exporter.run_once()["exported"] == 1 and exporter.run_once()["exported"] == 0
    auth, event_id, doc = sent[0]
    assert len(sent) == 1 and event_id == payload["event_id"] and auth == "ApiKey " + KEY
    assert doc["@timestamp"].endswith("Z") and doc["event"]["id"] == payload["event_id"]
    assert doc["event"]["action"] == "unauthorized_access" and doc["event"]["type"] == ["denied"]
    assert doc["event"]["kind"] == "alert" and doc["event"]["severity"] == 73
    assert doc["observer"]["name"] == "SG-R1" and doc["user"]["id"] == "Visitor_01"
    assert doc["secureguard"]["zone"] == "C2" and doc["labels"]["simulated"] == "true"
    assert doc["data_stream"] == {"type": "logs", "dataset": "secureguard.events", "namespace": "default"}
    assert KEY not in caplog.text


def test_robot_status_not_exported_by_default(exporting):
    robot, sent, _ = exporting
    robot.post(event(event_type="robot_status", severity="info", person=None,
                     robot={"speed_mps": 0, "heading_rad": 0, "camera_enabled": True, "sim_time_s": 1}))
    assert outbox(robot) == []


def test_retry_transient_then_success_and_409_duplicate(exporting):
    robot, sent, fake = exporting
    fake.replies = [503, 0, 201, 409]
    a = event(); robot.post(a)
    exporter = robot.client.app.state.exporter
    assert exporter.run_once()["retry"] == 1
    assert exporter.run_once()["retry"] == 0  # backoff: not due yet
    real = exporter.clock
    exporter.clock = lambda: real() + timedelta(hours=1)
    assert exporter.run_once()["retry"] == 1
    exporter.clock = lambda: real() + timedelta(hours=2)
    assert exporter.run_once()["exported"] == 1
    assert {s[1] for s in sent} == {a["event_id"]}  # same _id on every attempt
    b = event(); robot.post(b)
    assert exporter.run_once()["duplicate"] == 1
    assert len(robot.view("/api/v1/events").json()) == 2  # local records kept


def test_auth_error_stays_pending(exporting):
    robot, _, fake = exporting
    fake.replies = [401]
    robot.post(event())
    robot.client.app.state.exporter.run_once()
    assert outbox(robot)[0][1:] == ("pending", 1, "401")


def test_tampered_record_not_exported(exporting):
    robot, sent, _ = exporting
    a = event(); robot.post(a)
    with robot.client.app.state.store.connect() as db, db:
        db.execute("UPDATE events SET record=replace(record,'C2','C1')")
    assert robot.client.app.state.exporter.run_once()["skipped"] == 1 and sent == []


def test_https_transport_uses_create(monkeypatch):
    calls = []

    class Reply(io.BytesIO):
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(request, timeout, context):
        calls.append(request)
        if len(calls) == 2:
            raise urllib.error.HTTPError(request.full_url, 409, "conflict", {}, io.BytesIO(b"{}"))
        if len(calls) == 3:
            raise urllib.error.URLError("down")
        return Reply(b"{}")
    monkeypatch.setattr(elastic.urllib.request, "urlopen", urlopen)
    real_send = elastic.send.original  # conftest blocks elastic.send; urlopen is mocked here
    config = elastic.Config("https://x.example", KEY)
    assert [real_send(config, "SG-1", {"a": 1}) for _ in range(3)] == [201, 409, 0]
    assert calls[0].get_method() == "PUT"
    assert calls[0].full_url == "https://x.example/logs-secureguard.events-default/_create/SG-1"
    assert json.loads(calls[0].data) == {"a": 1}
