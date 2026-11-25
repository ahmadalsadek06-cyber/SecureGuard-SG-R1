"""End-to-end check of SG_EVENT_DESTINATION=elastic with a LOCAL fake Elasticsearch (no cloud data).

Starts an HTTPS server on 127.0.0.1 with a throw-away certificate (TLS verification stays ON:
the exporter trusts it via SG_ELASTIC_CA_FILE), runs the real Webots detection test in direct
mode, and checks that Elasticsearch-style `_create` requests arrived with the expected ECS fields.
Run with the backend venv (needs `cryptography` for the temporary certificate):

    backend\\.venv\\Scripts\\python scripts\\test_direct_elastic_webots.py
"""
from pathlib import Path
import http.server, json, os, ssl, sys, tempfile, threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend")); sys.path.insert(0, str(ROOT / "scripts"))
from app.provision import tls          # noqa: E402
import test_person_detection as detection   # noqa: E402

FAKE_KEY = "fake-key-for-local-test-only"
received, lock = [], threading.Lock()


class FakeElastic(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        ok_auth = self.headers.get("Authorization") == "ApiKey " + FAKE_KEY
        with lock:
            dup = any(r["path"] == self.path for r in received)
            received.append({"path": self.path, "auth_ok": ok_auth, "doc": json.loads(body)})
        status = 401 if not ok_auth else (409 if dup else 201)
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b"{}")


def main():
    work = Path(tempfile.mkdtemp(prefix="secureguard-direct-"))
    tls(work / "tls")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeElastic)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(work / "tls/server.pem", work / "tls/server-key.pem")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ.update(SG_EVENT_DESTINATION="elastic", ELASTICSEARCH_URL=f"https://127.0.0.1:{server.server_port}",
                      ELASTICSEARCH_API_KEY=FAKE_KEY, SG_ELASTIC_CA_FILE=str(work / "tls/ca.pem"),
                      SG_DIRECT_SPOOL_DIR=str(work / "spool"))
    detection.REPORT = work / "detection.json"           # never overwrite the project's report
    logic = detection.logic_checks()
    runtime, events, _, _ = detection.runtime_checks("C:/Program Files/Webots/msys64/mingw64/bin/webots.exe")
    server.shutdown()
    docs = [r["doc"] for r in received]
    actions = [d["event"]["action"] for d in docs]
    unauthorized = [d for d in docs if d["event"]["action"] == "unauthorized_access"]
    log_lines = " ".join(l for v in events.values() for l in v)
    checks = {
        "detection_logic": all(logic.values()),
        "detection_runtime": all(runtime.values()),
        "direct_mode_enabled": "direct Elastic: enabled" in log_lines,
        "requests_use_create_api": bool(received) and all(r["path"].startswith("/logs-secureguard.events-default/_create/SGR1-") for r in received),
        "api_key_header_correct": all(r["auth_ok"] for r in received),
        "unauthorized_access_exported": len(unauthorized) >= 1,
        "ecs_fields": all(d["user"]["id"] == "Visitor_01" and d["secureguard"]["zone"] == "C2" and d["event"]["severity"] == 73
                          and d["labels"]["simulated"] == "true" and d["observer"]["name"] == "SG-R1" for d in unauthorized),
        "person_detected_and_patrol_exported": "person_detected" in actions and "patrol_status" in actions,
        "robot_status_not_exported": "robot_status" not in actions,
        "integrity_label_honest": all(d["secureguard"]["integrity"] == "not_verified_direct_mode" for d in docs),
        "spool_empty_after_run": not list((work / "spool").glob("*.json")),
        "key_not_in_logs": FAKE_KEY not in log_lines,
    }
    for line in [l for l in log_lines.split(" SG-R1") if "direct Elastic" in l][:10]:
        print("log:", line[:200])
    print("spool files left:", len(list((work / "spool").glob("*.json"))))
    print(json.dumps({"documents": len(docs), "by_action": {a: actions.count(a) for a in set(actions)}, "checks": checks}, indent=2))
    raise SystemExit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
