"""LIVE test of direct mode: ONE Webots-generated unauthorized_access straight to Elasticsearch.

Run it yourself, in a PowerShell window where ELASTICSEARCH_URL and ELASTICSEARCH_API_KEY are set
(see DIRECT_ELASTIC_README.md). It refuses to run otherwise. Nothing secret is printed.

    python scripts\\live_direct_elastic_event.py

Headless Webots places SG-R1 in front of the Server Room door facing Visitor_01 for 4 s
(one alert; 30 s cooldown) and quits. Evidence printed:
  - the exporter's "created unauthorized_access SGR1-... (HTTP 201)" line = Elasticsearch accepted it;
  - optional search: if ELASTICSEARCH_READ_API_KEY (a separate READ-ONLY key) is set, the script
    searches for the event ID; otherwise check Kibana Discover with the printed query.
"""
from pathlib import Path
import json, os, re, ssl, subprocess, sys, tempfile, time, urllib.request

ROOT = Path(__file__).resolve().parents[1]
WORLD = ROOT / "worlds" / "secureguard_person_detection.wbt"
CTRL = ROOT / "controllers" / "sgr1_person_detection" / "sgr1_person_detection.py"
WEBOTS = "C:/Program Files/Webots/msys64/mingw64/bin/webots.exe"
STREAM = "logs-secureguard.events-default"

HARNESS = r'''
import controller, runpy, math
class Probe(controller.Supervisor):
    def __init__(self):
        super().__init__(); self.t = 0
        n = self.getSelf()
        n.getField('translation').setSFVec3f([6, -0.5, .005])
        n.getField('rotation').setSFRotation([0, 0, 1, math.pi/2]); n.resetPhysics()
    def step(self, period):
        status = super().step(period); self.t += period/1000
        if status != -1 and self.t >= 4:
            self.simulationQuit(0); return -1
        return status
controller.Supervisor = Probe
runpy.run_path(__ORIGINAL__, run_name='__main__')
'''


def main():
    url, key = os.environ.get("ELASTICSEARCH_URL", ""), os.environ.get("ELASTICSEARCH_API_KEY", "")
    if not url.startswith("https://") or not key:
        sys.exit("Not configured: set ELASTICSEARCH_URL (https) and ELASTICSEARCH_API_KEY in THIS window first. Nothing sent.")
    folder = Path(tempfile.mkdtemp(prefix="secureguard-live-direct-"))
    (folder / "worlds").mkdir(); (folder / "controllers/live_direct_probe").mkdir(parents=True)
    text = WORLD.read_text(encoding="utf-8")
    text = re.sub(r'EXTERNPROTO "[^"\n]*SecureGuardSGR1\.proto"', 'EXTERNPROTO "' + (ROOT / "protos/SecureGuardSGR1.proto").as_posix() + '"', text)
    text = re.sub(r'"(textures/[^"]+)"', lambda m: '"' + (WORLD.parent / m[1]).resolve().as_posix() + '"', text)
    (folder / "worlds/live.wbt").write_text(text.replace('controller "sgr1_person_detection"', 'controller "live_direct_probe"'), encoding="utf-8")
    (folder / "controllers/live_direct_probe/live_direct_probe.py").write_text(HARNESS.replace("__ORIGINAL__", repr(str(CTRL))))
    env = dict(os.environ, SG_EVENT_DESTINATION="elastic")
    run = subprocess.run([WEBOTS, "--batch", "--mode=realtime", "--no-rendering", "--minimize", "--stdout", "--stderr",
                          "--port=1281", str(folder / "worlds/live.wbt")], capture_output=True, text=True, timeout=180, env=env)
    log = run.stdout + run.stderr
    assert key not in log, "secret leaked into log"           # defensive: never print the key
    for line in log.splitlines():
        if "direct Elastic" in line or line.startswith("ALERT"):
            print(re.sub(r"\{.*", "{...}", line.strip())[:160])
    created = re.findall(r"(?:created|already present) unauthorized_access (SGR1-[0-9a-f]+) \(HTTP (201|409)\)", log)
    if len(created) != 1:
        sys.exit(f"Expected exactly 1 accepted unauthorized_access, found {len(created)}. See lines above.")
    event_id, status = created[0]
    print(f"\nElasticsearch accepted {event_id} (HTTP {status}).")
    print(f'Kibana Discover (data view logs-*):  event.id : "{event_id}"')
    read_key = os.environ.get("ELASTICSEARCH_READ_API_KEY")
    if not read_key:
        print("Visibility not checked by this script (no ELASTICSEARCH_READ_API_KEY). Use the Kibana query above.")
        return
    body = json.dumps({"query": {"term": {"event.id": event_id}}, "_source": ["event.id", "event.action", "secureguard.zone"]}).encode()
    for _ in range(10):
        req = urllib.request.Request(f"{url.rstrip('/')}/{STREAM}/_search", data=body, method="POST",
                                     headers={"Authorization": "ApiKey " + read_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as reply:
            hits = json.load(reply)["hits"]["hits"]
        if hits:
            print(f"VISIBLE in Elasticsearch: {len(hits)} document(s) with event.id {event_id}")
            return
        time.sleep(2)
    print("NOT visible yet after ~20 s; check Kibana Discover.")


if __name__ == "__main__":
    main()
