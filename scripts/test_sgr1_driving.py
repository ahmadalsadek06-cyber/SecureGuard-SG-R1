"""Focused SG-R1 driving check: 8 WASD combinations, release-to-stop, key dropouts.

Runs the person-detection controller (which uses sgr1_controller.Drive) in a temp
copy of worlds/secureguard_person_detection.wbt with injected held keys.
Writes worlds/secureguard_driving.runtime.json.
"""
from pathlib import Path
import json, re, subprocess, sys, tempfile

ROOT = Path(__file__).resolve().parents[1]
WORLD = ROOT/'worlds/secureguard_person_detection.wbt'
CTRL = ROOT/'controllers/sgr1_person_detection/sgr1_person_detection.py'
REPORT = ROOT/'worlds/secureguard_driving.runtime.json'

HARNESS = r'''
import controller, runpy, math, json
ORIGINAL = __ORIGINAL__
REPORT = __REPORT__
D = 2.0
# name, seconds, keys, reset; reset at x=-8 in the open corridor facing +x
# idle_* phases let the controller's own ramp return to rest before each teleport
TESTS = [(k, D, k, True) for k in ('W','S','A','D','WA','WD','SA','SD')] + [
         ('release_run', 2.5, 'W', True), ('release', 2.0, '', False),
         ('dropout', 2.5, 'W~', True), ('reverse_release_run', 2.0, 'S', True), ('reverse_release', 1.5, '', False)]
PHASES = [('settle', 0.5, '', True)]
for test in TESTS:
    if test[3]: PHASES.append(('idle_'+test[0], 1.0, '', False))
    PHASES.append(test)

class Keys:
    def __init__(s, o): s.o = o; s.q = []
    def enable(s, p): pass
    def getKey(s):
        if not s.q and not s.o.consumed:
            s.o.consumed = True; keys = PHASES[s.o.phase][2]
            if keys.endswith('~'):  # drop the key on every 4th sample
                keys = '' if s.o.steps % 4 == 3 else keys[:-1]
            s.q = [ord(c) for c in keys]
        return s.q.pop() if s.q else -1

class Probe(controller.Supervisor):
    def __init__(s):
        super().__init__(); s.phase = 0; s.t = 0; s.steps = 0; s.consumed = False; s.samples = []; s.res = {}
        s.reset()
    def getKeyboard(s): return Keys(s)
    def reset(s):
        n = s.getSelf(); n.getField('translation').setSFVec3f([-8, 0, .005])
        n.getField('rotation').setSFRotation([0, 0, 1, 0]); n.resetPhysics()
    def sample(s):
        n = s.getSelf(); p = n.getPosition(); o = n.getOrientation(); v = n.getVelocity()
        yaw = math.atan2(o[3], o[0])
        return dict(p=p, yaw=yaw, fwd=v[0]*math.cos(yaw)+v[1]*math.sin(yaw), wz=v[5],
                    speed=math.hypot(v[0], v[1]), tilt=math.acos(max(-1, min(1, o[8]))))
    def step(s, period):
        st = super().step(period)
        if st == -1: return -1
        s.consumed = False; s.steps += 1; s.t += period/1000
        s.samples.append(s.sample())
        name, dur, _, _ = PHASES[s.phase]
        if s.t >= dur:
            a, b = s.samples[0], s.samples[-1]
            c, sn = math.cos(a['yaw']), math.sin(a['yaw'])
            dx, dy = b['p'][0]-a['p'][0], b['p'][1]-a['p'][1]
            dyaw = sum(math.atan2(math.sin(q['yaw']-p['yaw']), math.cos(q['yaw']-p['yaw'])) for p, q in zip(s.samples, s.samples[1:]))
            acc = [abs(q['fwd']-p['fwd'])/(period/1000) for p, q in zip(s.samples, s.samples[1:])]
            yacc = [abs(q['wz']-p['wz'])/(period/1000) for p, q in zip(s.samples, s.samples[1:])]
            half = s.samples[len(s.samples)//2:]
            s.res[name] = dict(forward_m=dx*c+dy*sn, left_m=-dx*sn+dy*c, yaw_rad=dyaw,
                end_speed=b['speed'], max_tilt=max(q['tilt'] for q in s.samples),
                min_fwd=min(q['fwd'] for q in s.samples), max_fwd=max(q['fwd'] for q in s.samples),
                max_abs_wz=max(abs(q['wz']) for q in s.samples), p95_lin_accel=sorted(acc)[int(.95*len(acc))],
                p95_yaw_accel=sorted(yacc)[int(.95*len(yacc))],
                late_fwd_min=min(q['fwd'] for q in half), late_fwd_max=max(q['fwd'] for q in half))
            s.phase += 1; s.t = 0; s.samples = []
            if s.phase == len(PHASES):
                open(REPORT, 'w').write(json.dumps(s.res, indent=1)); s.simulationQuit(0); return -1
            if PHASES[s.phase][3]: s.reset()
        return st

controller.Supervisor = Probe
runpy.run_path(ORIGINAL, run_name='__main__')
'''


def checks(r):
    c = {}
    c['W_forward'] = r['W']['forward_m'] > 1.0 and abs(r['W']['yaw_rad']) < .1
    c['S_reverse'] = r['S']['forward_m'] < -0.5 and abs(r['S']['yaw_rad']) < .1
    c['A_pivot_left'] = r['A']['yaw_rad'] > .8 and abs(r['A']['forward_m']) < .15
    c['D_pivot_right'] = r['D']['yaw_rad'] < -.8 and abs(r['D']['forward_m']) < .15
    for k, fs, ls, ys in (('WA', 1, 1, 1), ('WD', 1, -1, -1), ('SA', -1, 1, -1), ('SD', -1, -1, 1)):
        x = r[k]
        c[k+'_curve'] = fs*x['forward_m'] > .3 and ls*x['left_m'] > .08 and ys*x['yaw_rad'] > .3
        # no pivot: never moves against the commanded travel direction
        c[k+'_no_pivot'] = (x['min_fwd'] > -.03) if fs > 0 else (x['max_fwd'] < .03)
    c['release_stops'] = r['release']['end_speed'] < .02 and abs(r['release']['yaw_rad']) < .05
    c['release_distance_lt_0_6m'] = 0 < r['release']['forward_m'] < .6
    c['reverse_release_stops'] = r['reverse_release']['end_speed'] < .02
    c['dropout_smooth'] = r['dropout']['late_fwd_min'] > .85
    c['smooth_accel'] = all(v['p95_lin_accel'] < 2.5 for k, v in r.items() if not k.startswith('idle'))
    c['stable_upright'] = all(v['max_tilt'] < .2 for v in r.values())
    return c


def main():
    folder = Path(tempfile.mkdtemp(prefix='secureguard-drive-'))
    (folder/'worlds').mkdir(); (folder/'controllers/drive_probe').mkdir(parents=True)
    text = WORLD.read_text()
    text = re.sub(r'"(\.\./protos/[^"]+|textures/[^"]+)"', lambda m: '"'+(WORLD.parent/m[1]).resolve().as_posix()+'"', text)
    (folder/'worlds/test.wbt').write_text(text.replace('controller "sgr1_person_detection"', 'controller "drive_probe"'))
    raw = folder/'raw.json'
    (folder/'controllers/drive_probe/drive_probe.py').write_text(
        HARNESS.replace('__ORIGINAL__', repr(str(CTRL))).replace('__REPORT__', repr(str(raw))))
    out = subprocess.run(['C:/Program Files/Webots/msys64/mingw64/bin/webots.exe', '--batch', '--mode=fast',
                          '--no-rendering', '--minimize', '--stdout', '--stderr', '--port=1251',
                          str(folder/'worlds/test.wbt')], capture_output=True, text=True, timeout=150)
    if 'Traceback' in out.stdout+out.stderr or not raw.exists():
        print((out.stdout+out.stderr)[-1500:]); sys.exit(1)
    r = json.loads(raw.read_text()); c = checks(r)
    REPORT.write_text(json.dumps({'checks': c, 'measurements': r}, indent=1)+'\n')
    failed = [k for k, v in c.items() if not v]
    print(f'{len(c)-len(failed)}/{len(c)} passed' + (' | FAILED: '+', '.join(failed) if failed else ''))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
