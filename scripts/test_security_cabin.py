"""Headless Webots check of worlds/secureguard_with_cabin.wbt (temporary copy; nothing is modified).

Checks: world loads without errors, SG-R1 spawns stable and clear, the robot can drive out of
the main entrance onto the exterior plaza without falling, the cabin's front wall and west
window glass block the robot, and the camera / overview images show the cabin.
Writes the report and two images to the folder given with --out (default: system temp).
Run with the backend bridge disabled so no events are sent anywhere.
"""
from pathlib import Path
import argparse, json, os, re, subprocess, tempfile

ROOT = Path(__file__).resolve().parents[1]
WORLD = ROOT / "worlds/secureguard_with_cabin.wbt"
CTRL = ROOT / "controllers/sgr1_person_detection/sgr1_person_detection.py"
WEBOTS = "C:/Program Files/Webots/msys64/mingw64/bin/webots.exe"

HARNESS = r'''
import controller, runpy, math, json
ORIGINAL, OUT = __ORIGINAL__, __OUT__
# name, seconds, key, reset [x, y, yaw] or None
PHASES = [("spawn", 1.5, "", None),
          ("exit_to_plaza", 7.0, "W", [0, -6, -math.pi/2]),
          ("front_wall", 4.0, "W", [3.75, -8.7, -math.pi/2]),
          ("west_window", 4.0, "W", [1.0, -11.63, 0.0]),
          ("snapshot", 1.0, "", [-1.0, -14.2, math.atan2(3.0, 5.0)])]

def quat_axis_angle(yaw, pitch):
    cy, sy, cp, sp = math.cos(yaw/2), math.sin(yaw/2), math.cos(pitch/2), math.sin(pitch/2)
    w, x, y, z = cy*cp, -sy*sp, cy*sp, sy*cp          # Rz(yaw) * Ry(pitch)
    s = math.sqrt(max(1e-12, 1 - w*w))
    return [x/s, y/s, z/s, 2*math.acos(max(-1, min(1, w)))]

class Keys:
    def __init__(s, o): s.o = o; s.q = []
    def enable(s, p): pass
    def getKey(s):
        if not s.q and not s.o.consumed:
            s.o.consumed = True; s.q = [ord(c) for c in PHASES[s.o.phase][2]]
        return s.q.pop() if s.q else -1

class Probe(controller.Supervisor):
    def __init__(s):
        super().__init__(); s.phase = 0; s.t = 0; s.consumed = False; s.res = {}; s.samples = []
    def getKeyboard(s): return Keys(s)
    def step(s, period):
        st = super().step(period)
        if st == -1: return -1
        s.consumed = False; s.t += period / 1000
        n = s.getSelf(); p = n.getPosition(); o = n.getOrientation(); v = n.getVelocity()
        s.samples.append((p, math.acos(max(-1, min(1, o[8]))), math.hypot(v[0], v[1])))
        name, dur, _, _ = PHASES[s.phase]
        if s.t >= dur:
            ps = [x[0] for x in s.samples]
            s.res[name] = dict(start=ps[0], end=ps[-1], min_z=min(q[2] for q in ps), max_tilt=max(x[1] for x in s.samples),
                               end_speed=s.samples[-1][2])
            if name == "snapshot":
                cam = s.getDevice("front_camera")
                cam.saveImage(OUT + "/camera_view.png", 100)
                vp = [s.getRoot().getField("children").getMFNode(i) for i in range(s.getRoot().getField("children").getCount())]
                vp = next(x for x in vp if x.getTypeName() == "Viewpoint")
                vp.getField("follow").setSFString("")
                vp.getField("position").setSFVec3f([-1.5, -17.5, 4.2])
                d = [4.0 + 1.5, -11.0 + 17.5, 1.2 - 4.2]
                vp.getField("orientation").setSFRotation(quat_axis_angle(math.atan2(d[1], d[0]), math.atan2(-d[2], math.hypot(d[0], d[1]))))
                super().step(period); super().step(period)
                s.exportImage(OUT + "/overview.png", 90)
                super().step(period)
            s.phase += 1; s.t = 0; s.samples = []
            if s.phase == len(PHASES):
                open(OUT + "/raw.json", "w").write(json.dumps(s.res)); s.simulationQuit(0); return -1
            reset = PHASES[s.phase][3]
            if reset:
                n.getField("translation").setSFVec3f([reset[0], reset[1], .005])
                n.getField("rotation").setSFRotation([0, 0, 1, reset[2]]); n.resetPhysics()
        return st

controller.Supervisor = Probe
runpy.run_path(ORIGINAL, run_name="__main__")
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=tempfile.mkdtemp(prefix="secureguard-cabin-out-"))
    out = Path(ap.parse_args().out); out.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="secureguard-cabin-"))
    (folder / "worlds").mkdir(); (folder / "controllers/cabin_probe").mkdir(parents=True)
    text = WORLD.read_text(encoding="utf-8")
    text = re.sub(r'"(\.\./protos/[^"]+|textures/[^"]+)"', lambda m: '"' + (WORLD.parent / m[1]).resolve().as_posix() + '"', text)
    (folder / "worlds/test.wbt").write_text(text.replace('controller "sgr1_person_detection"', 'controller "cabin_probe"'), encoding="utf-8")
    (folder / "controllers/cabin_probe/cabin_probe.py").write_text(
        HARNESS.replace("__ORIGINAL__", repr(str(CTRL))).replace("__OUT__", repr(out.as_posix())))
    env = dict(os.environ, SG_BACKEND_DISABLED="1")
    run = subprocess.run([WEBOTS, "--batch", "--mode=fast", "--minimize", "--stdout", "--stderr", "--port=1271",
                          str(folder / "worlds/test.wbt")], capture_output=True, text=True, timeout=240, env=env)
    log = run.stdout + run.stderr
    problems = [l.strip()[:200] for l in log.splitlines() if re.search(r"ERROR|Traceback|cannot|failed|not found", l, re.I)]
    r = json.loads((out / "raw.json").read_text())
    ok = lambda name: r[name]["max_tilt"] < 0.2 and r[name]["min_z"] > -0.05
    checks = {
        "world_loaded_without_errors": not problems,
        "controller_started": "Person Detection Ready" in log,
        "spawn_stable_and_clear": ok("spawn") and r["spawn"]["end_speed"] < 0.02 and abs(r["spawn"]["end"][0] - 5) < 0.05,
        "drives_out_onto_plaza": r["exit_to_plaza"]["end"][1] < -11.0 and ok("exit_to_plaza"),
        "cabin_front_wall_blocks": -9.75 + 0.30 < r["front_wall"]["end"][1] < -9.0 and r["front_wall"]["end_speed"] < 0.05,
        "west_window_glass_blocks": 2.5 - 0.45 < r["west_window"]["end"][0] < 2.5 - 0.30 and r["west_window"]["end_speed"] < 0.05,
        "stable_everywhere": all(ok(k) for k in r),
        "camera_image_saved": (out / "camera_view.png").exists(),
        "overview_image_saved": (out / "overview.png").exists(),
    }
    (out / "report.json").write_text(json.dumps({"checks": checks, "measurements": r, "log_problems": problems[:20]}, indent=2))
    print(json.dumps(checks, indent=2)); print("Output folder:", out)
    raise SystemExit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
