"""Real HTTPS round trip: Webots bridge (stdlib) -> uvicorn TLS backend, temp DB/secrets."""
import json
import socket
import threading
import time

import pytest
import uvicorn

from app.main import create_app
from backend_bridge import BackendBridge  # controllers/sgr1_person_detection
from tests.conftest import event


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path, secrets_dir):
    app = create_app(secrets_dir, tmp_path / "bridge.db", elastic_config=None)
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                            ssl_certfile=str(secrets_dir / "tls" / "server.pem"),
                            ssl_keyfile=str(secrets_dir / "tls" / "server-key.pem"))
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield app, port
    srv.should_exit = True
    thread.join(5)


def bridge_for(secrets_dir, port, logs):
    data = json.loads((secrets_dir / "robot.json").read_text())
    return BackendBridge(f"https://localhost:{port}", data["robot_id"], bytes.fromhex(data["secret"]),
                         secrets_dir / "tls" / "ca.pem", log=logs.append)


def wait(predicate, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_bridge_delivers_signed_events_over_verified_tls(server, secrets_dir):
    app, port = server
    logs = []
    bridge = bridge_for(secrets_dir, port, logs)
    payloads = [event(), event(event_type="person_detected", severity="info", zone="B1",
                               person={"person_id": "Student_01", "classification": "AUTHORIZED", "zone": "B1", "distance_m": 4.4}),
                event(event_type="robot_status", severity="info", person=None, zone="B1",
                      robot={"speed_mps": 0.8, "heading_rad": 0.1, "camera_enabled": True, "sim_time_s": 5.0})]
    for p in payloads:
        bridge.submit({k: v for k, v in p.items() if k not in ("event_id", "timestamp")})
    assert wait(lambda: bridge.stats["sent"] == 3), (bridge.stats, logs)
    views = app.state.store.list()
    assert len(views) == 3 and all(v["integrity"] == "verified" for v in views)
    bridge.close()


def test_submit_never_blocks_and_retries_when_backend_down(secrets_dir, tmp_path):
    logs = []
    bridge = bridge_for(secrets_dir, free_port(), logs)  # nothing listening
    start = time.perf_counter()
    for _ in range(100):
        bridge.submit(event())
    assert time.perf_counter() - start < 0.05  # control loop is not blocked by network
    assert wait(lambda: bridge.stats["retries"] >= 1)
    assert bridge.stats["sent"] == 0 and len(bridge.queue) == 100  # kept for retry
    assert "secret" not in repr(bridge) or "redacted" in repr(bridge)
    bridge.close(0)


def test_bridge_drops_invalid_event_and_continues(server, secrets_dir):
    _, port = server
    logs = []
    bridge = bridge_for(secrets_dir, port, logs)
    bridge.submit(event(severity="info"))  # invalid: unauthorized_access must be >= high
    bridge.submit(event())
    assert wait(lambda: bridge.stats["sent"] == 1 and bridge.stats["rejected"] == 1), bridge.stats
    assert any("HTTP 422" in line for line in logs)
    bridge.close()
