"""Run the backend on https://127.0.0.1:8443 (loopback only, TLS from app.provision)."""
import os
from pathlib import Path

import uvicorn

from .main import create_app

TLS = Path(os.getenv("SG_SECRETS_DIR", Path(__file__).resolve().parents[1] / "secrets")) / "tls"

if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.getenv("SG_PORT", "8443")),
                ssl_certfile=str(TLS / "server.pem"), ssl_keyfile=str(TLS / "server-key.pem"),
                proxy_headers=False, server_header=False, log_level="info")
