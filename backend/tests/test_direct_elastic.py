"""Direct robot -> Elasticsearch mode (controllers/sgr1_person_detection/direct_elastic.py).

Mocked responses only: no network, no real Elastic data. Spools live in pytest tmp dirs.
"""
import io
import json
import ssl
import threading
import time
import urllib.error

import pytest

import direct_elastic as de
from backend_bridge import BackendBridge
from app import elastic as backend_elastic

KEY = "fake-direct-test-key-0123456789abcdef"
URL = "https://elastic.example.test"


class Reply(io.BytesIO):
    def __init__(self, status): super().__init__(b"{}"); self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeElastic:
    """Scripted responses; records every request it receives."""
    def __init__(self, replies=(), delay=0.0):
        self.replies, self.delay, self.calls = list(replies), delay, []

    def __call__(self, request, timeout, context):
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname   # TLS verification on
        if self.delay: time.sleep(self.delay)
        self.calls.append(request)
        status = self.replies.pop(0) if self.replies else 201
        if status == 0:
            raise urllib.error.URLError("network down")
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(b"{}"))
        return Reply(status)


def event(**changes):
    base = {"event_type": "unauthorized_access", "severity": "high", "zone": "C2",
            "position": {"x": 6.8, "y": 3.0},
            "person": {"person_id": "Visitor_01", "classification": "UNAUTHORIZED", "zone": "C2",
                       "distance_m": 3.42, "role": "visitor", "badge_id": "SG-VIS-3001"},
            "message": "Visitor_01 unauthorized in Server Room (simulated)"}
    base.update(changes)
    return base


def wait(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred(): return True
        time.sleep(0.02)
    return False


@pytest.fixture
def make(tmp_path):
    made = []
    def factory(fake, **kw):
        logs = []
        ex = de.DirectElasticExporter(de.Config(URL, KEY), spool_dir=tmp_path / "spool", log=logs.append,
                                      opener=fake, sleep=lambda s: None, **kw)
        made.append(ex); ex.logs = logs
        return ex
    yield factory
    for ex in made: ex.running = False


# ---------------------------------------------------------------- configuration and selection
def test_configuration_env_only_https_and_redacted():
    assert de.Config.from_env({})[0] is None
    assert de.Config.from_env({"ELASTICSEARCH_URL": URL})[0] is None
    assert de.Config.from_env({"ELASTICSEARCH_URL": "http://x", "ELASTICSEARCH_API_KEY": KEY})[0] is None
    assert de.Config.from_env({"ELASTICSEARCH_URL": "https://u:p@x", "ELASTICSEARCH_API_KEY": KEY})[0] is None
    cfg, err = de.Config.from_env({"ELASTICSEARCH_URL": URL + "/", "ELASTICSEARCH_API_KEY": KEY})
    assert err is None and cfg.url == URL and KEY not in repr(cfg)
    ctx = de.tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_destination_defaults_to_backend(tmp_path, monkeypatch):
    logs = []
    monkeypatch.setattr(BackendBridge, "from_environment", classmethod(lambda cls, log: "BACKEND"))
    assert de.select_event_sink(logs.append, env={}) == "BACKEND"
    assert de.select_event_sink(logs.append, env={"SG_EVENT_DESTINATION": "backend"}) == "BACKEND"
    assert de.select_event_sink(logs.append, env={"SG_EVENT_DESTINATION": "bogus"}) == "BACKEND"
    assert any("unknown SG_EVENT_DESTINATION" in l for l in logs)
    assert de.select_event_sink(logs.append, env={"SG_EVENT_DESTINATION": "elastic"}) is None   # no credentials
    assert any("disabled" in l for l in logs) and not any(KEY in l for l in logs)
    sink = de.select_event_sink(logs.append, env={"SG_EVENT_DESTINATION": "elastic", "ELASTICSEARCH_URL": URL,
                                                  "ELASTICSEARCH_API_KEY": KEY, "SG_DIRECT_SPOOL_DIR": str(tmp_path / "s")})
    assert isinstance(sink, de.DirectElasticExporter)
    sink.running = False
    assert not any(KEY in l for l in logs)


# ---------------------------------------------------------------- document format
def test_document_preserves_backend_ecs_fields():
    e = event(event_id="SGR1-abc", timestamp="2026-11-18T10:00:00.000Z", robot_id="SG-R1",
              source="webots-sgr1-simulation", created_at="2026-11-18T10:00:00.010Z")
    direct = de.to_document(e)
    backend = backend_elastic.document(dict(e, received_at=e["created_at"]))
    same = {k: v for k, v in backend.items() if k != "secureguard"}
    assert {k: v for k, v in direct.items() if k != "secureguard"} == same
    assert direct["event"]["action"] == "unauthorized_access" and direct["event"]["severity"] == 73
    assert direct["data_stream"] == {"type": "logs", "dataset": "secureguard.events", "namespace": "default"}
    assert direct["user"]["id"] == "Visitor_01" and direct["secureguard"]["person"]["classification"] == "UNAUTHORIZED"
    assert direct["secureguard"]["integrity"] == "not_verified_direct_mode"     # honest: no backend HMAC


# ---------------------------------------------------------------- delivery
def test_creates_document_with_stable_id(make):
    fake = FakeElastic([201]); ex = make(fake)
    event_id = ex.submit(event())
    assert wait(lambda: ex.stats["created"] == 1)
    req = fake.calls[0]
    assert req.get_method() == "PUT"
    assert req.full_url == f"{URL}/logs-secureguard.events-default/_create/{event_id}"
    assert req.get_header("Authorization") == "ApiKey " + KEY
    body = json.loads(req.data)
    assert body["event"]["id"] == event_id and body["@timestamp"].endswith("Z")
    assert wait(lambda: not list(ex.spool.glob("*.json")))                   # spool cleared after 201


def test_conflict_409_is_duplicate_not_error(make):
    fake = FakeElastic([409]); ex = make(fake)
    ex.submit(event())
    assert wait(lambda: ex.stats["duplicate"] == 1) and ex.stats["rejected"] == 0


def test_transient_errors_retry_same_id(make):
    fake = FakeElastic([503, 0, 429, 408, 201]); ex = make(fake)
    ex.submit(event())
    assert wait(lambda: ex.stats["created"] == 1)
    assert ex.stats["retries"] == 4 and len({r.full_url for r in fake.calls}) == 1
    assert sum("temporary failure" in l for l in ex.logs) == 1                  # reported once, not spammed


def test_backoff_is_exponential(make):
    sleeps = []
    fake = FakeElastic([503, 503, 503, 201]); ex = make(fake)
    ex.sleep = sleeps.append
    ex.submit(event())
    assert wait(lambda: ex.stats["created"] == 1)
    assert sleeps == [1.0, 2.0, 4.0]


def test_auth_error_keeps_event_in_spool(make):
    fake = FakeElastic([401, 403, 201]); ex = make(fake)
    ex.submit(event())
    assert wait(lambda: ex.stats["created"] == 1)
    assert any("create_doc privilege" in l for l in ex.logs) and not any(KEY in l for l in ex.logs)


def test_bad_request_moves_to_rejected_and_reports(make):
    fake = FakeElastic([400]); ex = make(fake)
    eid = ex.submit(event())
    assert wait(lambda: ex.stats["rejected"] == 1)
    assert (ex.rejected / f"{eid}.json").exists()
    assert any("rejected (HTTP 400)" in l for l in ex.logs)


def test_submit_never_blocks_robot_loop(make):
    fake = FakeElastic(delay=0.5); ex = make(fake)
    start = time.perf_counter()
    for _ in range(100):
        ex.submit(event())
    assert time.perf_counter() - start < 0.1


def test_queue_overflow_is_spooled_and_reported(make):
    gate = threading.Event()
    class Blocking(FakeElastic):
        def __call__(self, request, timeout, context):
            gate.wait(5); return super().__call__(request, timeout, context)
    fake = Blocking(); ex = make(fake, max_queue=2)
    ids = [ex.submit(event()) for _ in range(6)]
    assert ex.stats["spooled_on_overflow"] >= 3
    assert any("queue full" in l for l in ex.logs)
    gate.set()
    assert wait(lambda: ex.stats["created"] == 6, timeout=10)
    assert {r.full_url.rsplit("/", 1)[1] for r in fake.calls} == set(ids)       # nothing lost, no duplicates


def test_spool_full_drop_is_counted(make):
    gate = threading.Event()
    class Blocking(FakeElastic):
        def __call__(self, request, timeout, context):
            gate.wait(5); return super().__call__(request, timeout, context)
    ex = make(Blocking(), max_queue=1, max_spool=1)
    for _ in range(5):
        ex.submit(event())
    assert ex.stats["dropped"] >= 1 and any("DROPPED" in l for l in ex.logs)
    gate.set()


def test_spool_survives_restart_and_has_no_secret(tmp_path):
    spool = tmp_path / "spool"
    first = de.DirectElasticExporter(de.Config(URL, KEY), spool_dir=spool, log=lambda m: None,
                                     opener=FakeElastic([0] * 1000), sleep=lambda s: time.sleep(0.01))
    eid = first.submit(event())
    assert wait(lambda: (spool / f"{eid}.json").exists())
    first.running = False
    text = (spool / f"{eid}.json").read_text()
    assert KEY not in text and "ApiKey" not in text and json.loads(text)["event_id"] == eid
    time.sleep(0.1)
    fake = FakeElastic([201]); logs = []
    second = de.DirectElasticExporter(de.Config(URL, KEY), spool_dir=spool, log=logs.append, opener=fake, sleep=lambda s: None)
    assert wait(lambda: second.stats["created"] >= 1)
    assert fake.calls[0].full_url.endswith(eid) and any("recovered" in l for l in logs)
    second.running = False


def test_robot_status_skipped_by_default_and_invalid_reported(make):
    fake = FakeElastic(); ex = make(fake)
    assert ex.submit(event(event_type="robot_status", severity="info", person=None,
                           robot={"speed_mps": 0, "heading_rad": 0, "camera_enabled": True, "sim_time_s": 1})) is None
    assert ex.stats["skipped_status"] == 1
    assert ex.submit(event(zone="Z9")) is None and ex.stats["invalid"] == 1
    assert any("invalid" in l for l in ex.logs)
    time.sleep(0.2)
    assert fake.calls == []


def test_close_reports_pending(make):
    ex = make(FakeElastic([0] * 1000))
    ex.sleep = lambda s: time.sleep(0.05)
    ex.submit(event())
    ex.close(timeout=0.3)
    assert any("left in spool" in l for l in ex.logs)
