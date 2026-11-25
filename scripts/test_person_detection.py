"""Focused checks: authorization logic (pure Python) + isolated Webots camera-recognition run.

Runs a temporary copy of worlds/secureguard_person_detection.wbt; source files are not modified.
Writes worlds/secureguard_person_detection.runtime.json.
"""
from pathlib import Path
import argparse
import json
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WORLD = ROOT/'worlds/secureguard_person_detection.wbt'
CTRL = ROOT/'controllers/sgr1_person_detection'
REPORT = ROOT/'worlds/secureguard_person_detection.runtime.json'
sys.path.insert(0, str(CTRL))
import person_auth as auth  # noqa: E402


def logic_checks():
    c = auth.load_config()
    gate = auth.AlertGate(c['alert_cooldown_s'])
    return {
        'zone_corridor': auth.zone_at(c, 9.5, 0.6) == 'B1',
        'zone_warehouse': auth.zone_at(c, 6.5, -3.5) == 'B2',
        'zone_server_room': auth.zone_at(c, 6.8, 3.0) == 'C2',
        'zone_wall_unmapped': auth.zone_at(c, 0, 1.5) is None,
        'employee_server_authorized': auth.classify(c, 'Employee_01', 'C2') == auth.AUTHORIZED,
        'student_server_unauthorized': auth.classify(c, 'Student_01', 'C2') == auth.UNAUTHORIZED,
        'student_lab_authorized': auth.classify(c, 'Student_01', 'C1') == auth.AUTHORIZED,
        'visitor_server_unauthorized': auth.classify(c, 'Visitor_01', 'C2') == auth.UNAUTHORIZED,
        'unregistered_unknown': auth.classify(c, 'Intruder_99', 'B1') == auth.UNKNOWN,
        'no_zone_unknown': auth.classify(c, 'Visitor_01', None) == auth.UNKNOWN,
        'cooldown': [gate.allow('V', t) for t in (0, 10, 29.9, 30, 45, 61)] == [True, False, False, True, False, True],
    }


HARNESS = r'''
import controller, runpy, math
ORIGINAL = __ORIGINAL__
# name, seconds, held key, reset pose [x, y, yaw] or None
PHASES = [('settle', 1, None, [5, 0, 0]),  # pin spawn: the saved world may hold a GUI-moved robot
          ('student', 2, None, [5, 0, 0]),
          ('visitor', 2, None, [6, -0.5, math.pi/2]),
          ('visitor_hold', 26, None, None),
          ('visitor_after_cooldown', 6, None, None),
          ('occluded_visitor', 3, None, [2, 0, math.atan2(3, 4.8)]),
          ('employee', 3, None, [8, 0, -math.pi/2]),
          ('drive', 3, 'W', [5, 0, 0]),
          ('drive_turn', 2, 'WA', None),
          ('camera_off', 1, 'C', [5, 0, 0]),
          ('camera_off_hold', 2, None, None)]

class Keys:
    def __init__(self, o): self.o = o; self.queue = []
    def enable(self, p): pass
    def getKey(self):
        if not self.queue and not self.o.consumed:
            self.o.consumed = True
            self.queue = [ord(k) for k in (PHASES[self.o.phase][2] or '')]
        return self.queue.pop() if self.queue else -1

class Probe(controller.Supervisor):
    def __init__(self):
        super().__init__(); self.phase = 0; self.t = 0; self.consumed = False; self.start = None
        print('PHASE settle', flush=True)
        n = self.getSelf(); n.getField('translation').setSFVec3f([5, 0, .005])
        n.getField('rotation').setSFRotation([0, 0, 1, 0]); n.resetPhysics()
    def getKeyboard(self): return Keys(self)
    def step(self, period):
        status = super().step(period)
        if status == -1: return -1
        self.consumed = False
        n = self.getSelf()
        if self.start is None: self.start = n.getPosition()
        self.t += period/1000
        name, duration, key, _ = PHASES[self.phase]
        if self.t >= duration:
            p = n.getPosition(); o = n.getOrientation()
            print('POSE %s %.3f %.3f %.3f %.3f' % (name, p[0], p[1], p[2], math.atan2(o[3], o[0])), flush=True)
            self.phase += 1; self.t = 0
            if self.phase == len(PHASES):
                self.simulationQuit(0); return -1
            name, _, _, reset = PHASES[self.phase]
            print('PHASE ' + name, flush=True)
            if reset:
                n.getField('translation').setSFVec3f([reset[0], reset[1], .005])
                n.getField('rotation').setSFRotation([0, 0, 1, reset[2]])
                n.resetPhysics()
        return status

controller.Supervisor = Probe
runpy.run_path(ORIGINAL, run_name='__main__')
'''


def runtime_checks(webots):
    folder = Path(tempfile.mkdtemp(prefix='secureguard-person-'))
    (folder/'worlds').mkdir(); (folder/'controllers/pd_probe').mkdir(parents=True)
    text = WORLD.read_text()
    text = re.sub(r'"(\.\./protos/[^"]+|textures/[^"]+)"', lambda m: '"'+(WORLD.parent/m[1]).resolve().as_posix()+'"', text)
    text = text.replace('controller "sgr1_person_detection"', 'controller "pd_probe"')
    (folder/'worlds/test.wbt').write_text(text)
    (folder/'controllers/pd_probe/pd_probe.py').write_text(
        HARNESS.replace('__ORIGINAL__', repr(str(CTRL/'sgr1_person_detection.py'))))
    r = subprocess.run([webots, '--batch', '--mode=fast', '--no-rendering', '--minimize', '--stdout', '--stderr',
                        '--port=1241', str(folder/'worlds/test.wbt')], capture_output=True, text=True, timeout=300)
    phase, events, poses = 'settle', {}, {}
    for line in (r.stdout+r.stderr).splitlines():
        line = re.sub(r'^\[[^\]]+\]\s*', '', line.strip())
        if line.startswith('PHASE '):
            phase = line.split()[1]
        elif line.startswith('POSE '):
            f = line.split(); poses[f[1]] = [float(v) for v in f[2:]]
        elif line.startswith(('DETECT', 'ALERT', 'LOST', 'SG-R1', 'SecureGuard', 'Traceback', 'Error')) or 'Error' in line:
            events.setdefault(phase, []).append(line)
    ev = lambda p: ' '.join(events.get(p, []))
    alerts = [l for p in events for l in events[p] if l.startswith('ALERT')]
    alert = json.loads(alerts[0].split(' ', 2)[2]) if alerts else {}
    dist = re.search(r'DETECT Student_01 \| AUTHORIZED \| zone B1 [^|]*\| ([\d.]+) m', ev('settle')+' '+ev('student'))  # same spawn pose
    checks = {
        'controller_started': 'Person Detection Ready' in ev('settle') or 'Person Detection Ready' in r.stdout,
        'no_python_errors': 'Traceback' not in r.stdout+r.stderr,
        'student_detected_authorized_B1': dist is not None,
        'student_distance_plausible': bool(dist) and 3.8 < float(dist[1]) < 4.8,
        'visitor_detected_unauthorized_C2': 'DETECT Visitor_01 | UNAUTHORIZED | zone C2' in ev('visitor'),
        'alert_fired': 'ALERT unauthorized_access' in ev('visitor'),
        'alert_payload': alert.get('person') == 'Visitor_01' and alert.get('zone') == 'C2' and alert.get('event') == 'unauthorized_access',
        'alert_cooldown_suppresses': 'ALERT' not in ev('visitor_hold'),
        'alert_repeats_after_30s': ev('visitor_after_cooldown').count('ALERT') == 1,
        'occluded_visitor_not_detected': 'Visitor_01' not in ev('occluded_visitor').replace('LOST Visitor_01', ''),
        'employee_detected_authorized_B2': 'DETECT Employee_01 | AUTHORIZED | zone B2' in ev('employee'),
        'wasd_drive_forward': poses.get('drive', [0])[0] - 5 > 0.8,
        'simultaneous_w_a_turns_and_moves': poses.get('drive_turn', [0]*4)[3] > 0.3 and poses['drive_turn'][0] > poses['drive'][0],
        'camera_toggle_off': 'camera+detection off' in ev('camera_off'),
        'no_detection_when_camera_off': 'DETECT' not in ev('camera_off_hold'),
        'robot_upright': all(-0.05 < p[2] < 0.05 for p in poses.values()),
    }
    return checks, events, poses, r.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--webots', default='C:/Program Files/Webots/msys64/mingw64/bin/webots.exe')
    a = ap.parse_args()
    logic = logic_checks()
    runtime, events, poses, code = runtime_checks(a.webots)
    REPORT.write_text(json.dumps({'logic': logic, 'runtime': runtime, 'events_by_phase': events, 'poses': poses,
                                  'webots_exit_code': code,
                                  'note': 'Simulated identity tags via Webots Camera Recognition; not facial recognition.'}, indent=2)+'\n')
    failed = [k for k, v in {**logic, **runtime}.items() if not v]
    print(f'logic {sum(logic.values())}/{len(logic)} | runtime {sum(runtime.values())}/{len(runtime)}')
    print('FAILED: '+', '.join(failed) if failed else 'ALL PASSED')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
