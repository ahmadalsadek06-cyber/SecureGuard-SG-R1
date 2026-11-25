"""Non-blocking SG-R1 -> SecureGuard backend bridge (Python stdlib only).

submit() never blocks the Webots control loop: events go into a bounded queue that a
daemon thread delivers over HTTPS with per-request HMAC signatures (robot secret never sent).
Retries 429/5xx/network failures with backoff; drops events the backend rejects as invalid.
Enabled automatically when backend/secrets/robot.json exists; set SG_BACKEND_DISABLED=1 to turn off.
Events are simulated detections; identities are simulated tags, not facial recognition.
"""
import collections
import hashlib
import hmac
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SECRETS = Path(os.getenv("SG_SECRETS_DIR", PROJECT / "backend" / "secrets"))
PATH = "/api/v1/events"
MAX_QUEUE = 500


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_event_id():
    return "SGR1-" + uuid.uuid4().hex


class BackendBridge:
    def __init__(self, url, robot_id, secret: bytes, ca_file, log=print, opener=None):
        self.url, self.robot_id, self._secret = url.rstrip("/"), robot_id, secret
        self.context = ssl.create_default_context(cafile=str(ca_file)) if ca_file else ssl.create_default_context()
        self.log, self.opener = log, opener or urllib.request.urlopen
        self.queue = collections.deque()
        self.cv = threading.Condition()
        self.stats = {"sent": 0, "duplicate": 0, "rejected": 0, "dropped": 0, "retries": 0}
        self.running = True
        self.thread = threading.Thread(target=self._run, name="sg-backend-bridge", daemon=True)
        self.thread.start()

    def __repr__(self):
        return f"BackendBridge({self.url!r}, {self.robot_id!r}, secret=<redacted>)"

    @classmethod
    def from_environment(cls, log=print):
        if os.getenv("SG_BACKEND_DISABLED") == "1":
            return None
        credential = SECRETS / "robot.json"
        if not credential.exists():
            log("SG-R1 backend bridge: disabled (no backend/secrets/robot.json)")
            return None
        data = json.loads(credential.read_text())
        url = os.getenv("SG_BACKEND_URL", "https://127.0.0.1:8443")
        if not url.startswith("https://"):
            log("SG-R1 backend bridge: disabled (SG_BACKEND_URL must be https)")
            return None
        bridge = cls(url, data["robot_id"], bytes.fromhex(data["secret"]), SECRETS / "tls" / "ca.pem", log)
        log(f"SG-R1 backend bridge: enabled -> {url}")
        return bridge

    def submit(self, event: dict):
        """Queue one event (non-blocking). Oldest events are dropped if the queue is full."""
        event.setdefault("event_id", new_event_id())
        event.setdefault("timestamp", utc_now())
        event.setdefault("robot_id", self.robot_id)
        event.setdefault("source", "webots-sgr1-simulation")
        with self.cv:
            if len(self.queue) >= MAX_QUEUE:
                self.queue.popleft(); self.stats["dropped"] += 1
            self.queue.append(event)
            self.cv.notify()
        return event["event_id"]

    def close(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while self.queue and time.monotonic() < deadline:
            time.sleep(0.05)
        self.running = False
        with self.cv:
            self.cv.notify()

    def _post(self, event):
        body = json.dumps(event, separators=(",", ":")).encode()
        stamp, nonce = str(int(time.time())), secrets.token_urlsafe(24)
        message = "\n".join(["POST", PATH, stamp, nonce, hashlib.sha256(body).hexdigest()]).encode()
        request = urllib.request.Request(self.url + PATH, data=body, method="POST", headers={
            "Content-Type": "application/json", "X-SG-Robot-Id": self.robot_id, "X-SG-Timestamp": stamp,
            "X-SG-Nonce": nonce, "X-SG-Signature": hmac.new(self._secret, message, hashlib.sha256).hexdigest()})
        try:
            with self.opener(request, timeout=5, context=self.context) as reply:
                return reply.status
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0

    def _run(self):
        backoff, warned = 0.5, False
        while self.running:
            with self.cv:
                while self.running and not self.queue:
                    self.cv.wait(0.5)
                if not self.running:
                    return
                event = self.queue[0]
            status = self._post(event)
            if status in (200, 201, 409, 400, 401, 403, 413, 422):
                with self.cv:
                    if self.queue and self.queue[0] is event:
                        self.queue.popleft()
                key = {200: "duplicate", 201: "sent"}.get(status, "rejected")
                self.stats[key] += 1
                if key == "rejected":
                    self.log(f"SG-R1 backend rejected {event['event_type']} {event['event_id']}: HTTP {status}")
                backoff, warned = 0.5, False
            else:  # 0 network, 429, 5xx: keep at head, retry later
                self.stats["retries"] += 1
                if not warned:
                    self.log(f"SG-R1 backend unavailable (HTTP {status or 'network'}); queued events will retry")
                    warned = True
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
