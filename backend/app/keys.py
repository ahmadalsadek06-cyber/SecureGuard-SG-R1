"""Key loading, record integrity (HMAC-SHA-256) and evidence encryption (AES-256-GCM).

Keys live in backend/secrets/ (created by `python -m app.provision`) or come from
environment variables SG_HMAC_KEY / SG_AES_KEY (64 hex chars). The HMAC and AES keys
must differ. Nothing here logs or returns key material.

Integrity meaning: a valid tag shows the stored record has not changed since THIS
backend accepted it. It does not prove that a simulated camera observation is real.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def read_hex_key(name: str, env_var: str, secrets_dir: Path) -> bytes:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        path = secrets_dir / f"{name}.key"
        if not path.exists():
            raise RuntimeError(f"Missing {name} key: run `python -m app.provision` or set {env_var}.")
        raw = path.read_text().strip()
    if not HEX64.fullmatch(raw):
        raise RuntimeError(f"{env_var} must be 64 hex characters (256 bits).")
    return bytes.fromhex(raw)


def write_new_secret(path: Path, text: str):
    """Create a secret file exclusively; never overwrite an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def new_hex_key() -> str:
    return secrets.token_hex(32)


class Keys:
    def __init__(self, hmac_key: bytes, aes_key: bytes):
        if len(hmac_key) != 32 or len(aes_key) != 32:
            raise RuntimeError("HMAC and AES keys must be 32 bytes.")
        if hmac.compare_digest(hmac_key, aes_key):
            raise RuntimeError("Use independent HMAC and AES keys.")
        self._hmac = hmac_key
        self._aes = AESGCM(aes_key)

    def __repr__(self):
        return "Keys(<redacted>)"

    @classmethod
    def load(cls, secrets_dir: Path):
        return cls(read_hex_key("hmac", "SG_HMAC_KEY", secrets_dir), read_hex_key("aes", "SG_AES_KEY", secrets_dir))

    def tag(self, record: dict) -> str:
        return hmac.new(self._hmac, canonical(record), hashlib.sha256).hexdigest()

    def verify(self, record: dict, tag: str) -> bool:
        return hmac.compare_digest(self.tag(record), tag or "")

    def seal(self, value: dict, aad: str) -> str:
        nonce = secrets.token_bytes(12)  # random 96-bit nonce per encryption
        return base64.b64encode(nonce + self._aes.encrypt(nonce, canonical(value), aad.encode())).decode()

    def open(self, sealed: str, aad: str) -> dict:
        data = base64.b64decode(sealed)
        return json.loads(self._aes.decrypt(data[:12], data[12:], aad.encode()))
