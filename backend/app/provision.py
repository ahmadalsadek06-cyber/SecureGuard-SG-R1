"""Create backend secrets once (never overwrites): HMAC key, AES key, robot credential,
dashboard token and a localhost-only TLS certificate signed by a throwaway local CA.

    .\\.venv\\Scripts\\python -m app.provision

Prints file locations only, never secret values.
"""
import hashlib
import ipaddress
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .keys import new_hex_key, write_new_secret

BACKEND = Path(__file__).resolve().parents[1]
HOSTS = ["localhost"]
IPS = ["127.0.0.1", "::1"]


def tls(folder: Path):
    if (folder / "server.pem").exists():
        return False
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SecureGuard SG-R1 local CA")])
    permitted = [x509.DNSName(h) for h in HOSTS] + [x509.IPAddress(ipaddress.ip_network(ip)) for ip in IPS]
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
          .not_valid_after(now + timedelta(days=365))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=180))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in HOSTS] +
                                                       [x509.IPAddress(ipaddress.ip_address(ip)) for ip in IPS]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (folder / "server.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    write_new_secret(folder / "server-key.pem", key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    return True  # CA private key is discarded: it can never sign another certificate


def provision(secrets_dir: Path, robot_id: str = "SG-R1"):
    created = []
    for name in ("hmac", "aes"):
        path = secrets_dir / f"{name}.key"
        if not path.exists():
            write_new_secret(path, new_hex_key()); created.append(path.name)
    robot = secrets_dir / "robot.json"
    if not robot.exists():
        write_new_secret(robot, json.dumps({"robot_id": robot_id, "secret": secrets.token_hex(32)}))
        created.append(robot.name)
    token_file = secrets_dir / "dashboard_token.txt"
    if not token_file.exists():
        token = secrets.token_urlsafe(32)
        write_new_secret(token_file, token + "\n")
        write_new_secret(secrets_dir / "dashboard_token.sha256", hashlib.sha256(token.encode()).hexdigest())
        created.append(token_file.name)
    if tls(secrets_dir / "tls"):
        created.append("tls/")
    return created


if __name__ == "__main__":
    folder = BACKEND / "secrets"
    made = provision(folder)
    print("Secrets folder:", folder)
    print("Created:", ", ".join(made) if made else "nothing (all present, unchanged)")
    print("Dashboard token: open secrets\\dashboard_token.txt (do not share).")
    sys.exit(0)
