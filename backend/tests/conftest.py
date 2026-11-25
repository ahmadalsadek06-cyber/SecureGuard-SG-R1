import hashlib
import json
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent / "controllers" / "sgr1_person_detection"))

from app import auth  # noqa: E402
from app.main import create_app  # noqa: E402
from app.provision import provision  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_elastic(monkeypatch):
    """Automated tests never reach a real Elastic project."""
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("ELASTICSEARCH_API_KEY", raising=False)
    for name in ("SG_HMAC_KEY", "SG_AES_KEY", "SG_SECRETS_DIR", "SG_DB"):
        monkeypatch.delenv(name, raising=False)

    def blocked(*_a, **_k):
        raise AssertionError("real Elastic call attempted in tests")
    from app import elastic
    blocked.original = getattr(elastic.send, "original", elastic.send)
    monkeypatch.setattr(elastic, "send", blocked)


@pytest.fixture(scope="session")
def secrets_dir(tmp_path_factory):
    folder = tmp_path_factory.mktemp("secrets")
    provision(folder)
    return folder


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def event(**changes):
    base = {"event_id": "SGR1-" + secrets.token_hex(8), "event_type": "unauthorized_access", "robot_id": "SG-R1",
            "timestamp": now_iso(), "severity": "high", "zone": "C2", "position": {"x": 6.8, "y": 3.0},
            "person": {"person_id": "Visitor_01", "classification": "UNAUTHORIZED", "role": "visitor",
                       "badge_id": "SG-VIS-3001", "zone": "C2", "distance_m": 3.42},
            "message": "Visitor_01 unauthorized in Server Room (simulated)"}
    base.update(changes)
    return base


class Robot:
    """Signs requests exactly like the Webots bridge."""
    def __init__(self, client, secrets_dir):
        self.client = client
        data = json.loads((secrets_dir / "robot.json").read_text())
        self.robot_id, self.secret = data["robot_id"], bytes.fromhex(data["secret"])
        self.token = (secrets_dir / "dashboard_token.txt").read_text().strip()

    def post(self, payload, *, stamp=None, nonce=None, secret=None, robot_id=None, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        stamp = str(stamp if stamp is not None else int(time.time()))
        nonce = nonce or secrets.token_urlsafe(18)
        sig = auth.sign(secret or self.secret, "POST", "/api/v1/events", stamp, nonce, body)
        return self.client.post("/api/v1/events", content=body, headers={
            "Content-Type": "application/json", "X-SG-Robot-Id": robot_id or self.robot_id,
            "X-SG-Timestamp": stamp, "X-SG-Nonce": nonce, "X-SG-Signature": sig})

    def view(self, path):
        return self.client.get(path, headers={"Authorization": "Bearer " + self.token})


@pytest.fixture
def make_robot(tmp_path, secrets_dir):
    clients = []

    def factory(**kwargs):
        app = create_app(secrets_dir, tmp_path / f"events{len(clients)}.db", **{"elastic_config": None, **kwargs})
        client = TestClient(app, base_url="https://testserver")
        client.__enter__()
        clients.append(client)
        return Robot(client, secrets_dir)
    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def robot(make_robot):
    return make_robot()


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()
