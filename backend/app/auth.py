"""Robot request signing, replay window, rate limiting and viewer token auth.

Robot requests carry:
  X-SG-Robot-Id, X-SG-Timestamp (unix seconds), X-SG-Nonce (16-64 url-safe chars),
  X-SG-Signature = hex HMAC-SHA-256(robot_secret, METHOD\\nPATH\\nTIMESTAMP\\nNONCE\\nSHA256(body)).
The robot secret never travels on the wire; HTTPS is still required for all requests.
"""
import hashlib
import hmac
import json
import re
import threading
import time
from pathlib import Path

NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def signing_message(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    return "\n".join([method.upper(), path, timestamp, nonce, hashlib.sha256(body).hexdigest()]).encode()


def sign(secret: bytes, method, path, timestamp, nonce, body) -> str:
    return hmac.new(secret, signing_message(method, path, timestamp, nonce, body), hashlib.sha256).hexdigest()


class RobotCredentials:
    """robot_id -> secret, loaded from secrets/robot.json ({"robot_id": ..., "secret": hex})."""
    def __init__(self, entries: dict):
        self._secrets = {rid: bytes.fromhex(secret) for rid, secret in entries.items()}

    def __repr__(self):
        return f"RobotCredentials({sorted(self._secrets)}, secrets=<redacted>)"

    @classmethod
    def load(cls, secrets_dir: Path):
        path = secrets_dir / "robot.json"
        if not path.exists():
            raise RuntimeError("Missing robot credential: run `python -m app.provision`.")
        data = json.loads(path.read_text())
        return cls({data["robot_id"]: data["secret"]})

    def secret(self, robot_id):
        return self._secrets.get(robot_id)


class RateLimiter:
    """Token bucket per key. Thread-safe, in-memory (resets on restart)."""
    def __init__(self, rate_per_s: float, burst: int):
        self.rate, self.burst = rate_per_s, burst
        self.buckets = {}
        self.lock = threading.Lock()

    def allow(self, key) -> bool:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1:
                self.buckets[key] = (tokens, now)
                return False
            self.buckets[key] = (tokens - 1, now)
            return True


def viewer_token_hash(secrets_dir: Path) -> str:
    path = secrets_dir / "dashboard_token.sha256"
    if not path.exists():
        raise RuntimeError("Missing dashboard token: run `python -m app.provision`.")
    return path.read_text().strip()


def token_matches(presented: str, expected_sha256: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(presented.encode()).hexdigest(), expected_sha256)
