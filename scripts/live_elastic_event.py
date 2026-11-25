"""Live check: ONE Webots-generated unauthorized_access -> backend -> Elastic.

Run while the backend is running WITH Elastic enabled (see backend/README.md):
    python scripts\\live_elastic_event.py

Headless Webots runs the real person-detection controller (and its backend bridge) on a temp
copy of the world, places SG-R1 facing Visitor_01 in the Server Room for a few seconds (one
alert, 30 s cooldown), then quits. It then reads the backend API and the local outbox
(read-only) to report whether Elastic acknowledged the document. Prints no secrets.
"""
import json
import os
import re
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
WORLD = ROOT / "worlds" / "secureguard_person_detection.wbt"
CTRL = ROOT / "controllers" / "sgr1_person_detection" / "sgr1_person_detection.py"
WEBOTS = "C:/Program Files/Webots/msys64/mingw64/bin/webots.exe"
BASE = os.getenv("SG_BACKEND_URL", "https://localhost:8443").rstrip("/")  # same var the Webots bridge uses
CTX = ssl.create_default_context(cafile=str(BACKEND / "secrets" / "tls" / "ca.pem"))

HARNESS = r'''
import controller, runpy, math
ORIGINAL = __ORIGINAL__
class Probe(controller.Supervisor):
    def __init__(self):
        super().__init__(); self.t = 0
        n = self.getSelf()
        n.getField('translation').setSFVec3f([6, -0.5, .005])
        n.getField('rotation').setSFRotation([0, 0, 1, math.pi/2]); n.resetPhysics()
    def step(self, period):
        status = super().step(period)
        self.t += period/1000
        if status != -1 and self.t >= 4:
            self.simulationQuit(0); return -1
        return status
controller.Supervisor = Probe
runpy.run_path(ORIGINAL, run_name='__main__')
'''


def api(path):
    token = (BACKEND / "secrets" / "dashboard_token.txt").read_text().strip()
    request = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + token})
    return json.load(urllib.request.urlopen(request, context=CTX, timeout=5))


def main():
    try:
        health = json.load(urllib.request.urlopen(BASE + "/healthz", context=CTX, timeout=5))
    except OSError:
        sys.exit(f"Backend not reachable at {BASE} - start it first.")
    if health.get("elastic_export") != "enabled":
        sys.exit("Backend is running with Elastic export DISABLED - restart it with the Elastic variables.")
    before = {e["event_id"] for e in api("/api/v1/events?event_type=unauthorized_access&limit=500")}

    folder = Path(tempfile.mkdtemp(prefix="secureguard-live-"))
    (folder / "worlds").mkdir(); (folder / "controllers/live_probe").mkdir(parents=True)
    text = WORLD.read_text()
    text = re.sub(r'"(\.\./protos/[^"]+|textures/[^"]+)"', lambda m: '"' + (WORLD.parent / m[1]).resolve().as_posix() + '"', text)
    (folder / "worlds/live.wbt").write_text(text.replace('controller "sgr1_person_detection"', 'controller "live_probe"'))
    (folder / "controllers/live_probe/live_probe.py").write_text(HARNESS.replace("__ORIGINAL__", repr(str(CTRL))))
    run = subprocess.run([WEBOTS, "--batch", "--mode=realtime", "--no-rendering", "--minimize", "--stdout", "--stderr",
                          "--port=1261", str(folder / "worlds/live.wbt")], capture_output=True, text=True, timeout=120)
    lines = [l for l in (run.stdout + run.stderr).splitlines() if re.search(r"ALERT|backend|Traceback", l)]
    print("Webots:", *[re.sub(r'\{.*', '{...}', l)[:120] for l in lines], sep="\n  ")

    new = []
    for _ in range(20):
        new = [e for e in api("/api/v1/events?event_type=unauthorized_access&limit=500") if e["event_id"] not in before]
        if new:
            break
        time.sleep(0.5)
    if len(new) != 1:
        sys.exit(f"Expected exactly 1 new unauthorized_access, found {len(new)}.")
    event = new[0]
    print(f"Backend: {event['event_id']} | {event['person']['person_id']} {event['person']['classification']} "
          f"in {event['zone']} | integrity {event['integrity']}")
    row = None
    for _ in range(40):  # exporter wakes immediately; allow for retries/backoff
        with sqlite3.connect(f"file:{BACKEND / 'data' / 'secureguard_events.sqlite3'}?mode=ro", uri=True) as db:
            row = db.execute("SELECT state,attempts,last_status FROM elastic_outbox WHERE event_id=?",
                             (event["event_id"],)).fetchone()
        if row and row[0] != "pending":
            break
        time.sleep(0.5)
    print("Outbox:", row)
    if row and row[0] == "exported" and row[2] == "201":
        print("Elastic acknowledged creation (HTTP 201). Confirm visibility in Kibana Discover with:")
    else:
        print("NOT exported yet (see last_status above). Kibana query for later:")
    print(f'  data_stream.dataset : "secureguard.events" and event.id : "{event["event_id"]}"')


if __name__ == "__main__":
    main()
