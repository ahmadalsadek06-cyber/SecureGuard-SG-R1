"""Run the unchanged controller with scripted keyboard input in an isolated Webots project.

This is a physics/controller smoke test, not a claim of human keyboard testing.
The delivered world and controller are never edited. Temporary artifacts are retained
in the OS temporary directory for inspection.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import controller
import json
import math
import runpy
from pathlib import Path

ORIGINAL = __ORIGINAL__
REPORT = __REPORT__
PHASES = [('settle', None, 30), ('forward', 'W', 90), ('backward', 'S', 90),
          ('left', 'A', 30), ('right', 'D', 30), ('wall', 'W', 180)]

class Keys:
    def __init__(self, owner): self.owner=owner
    def enable(self, period): pass
    def getKey(self):
        key=PHASES[self.owner.phase][1]
        return ord(key) if key else -1

class Probe(controller.Supervisor):
    def __init__(self):
        super().__init__()
        self.phase=0
        self.tick=0
        self.results={}
        self.start=None
    def getKeyboard(self): return Keys(self)
    def pose(self):
        node=self.getSelf()
        orientation=node.getOrientation()
        return {'position':node.getPosition(), 'yaw':math.atan2(orientation[3],orientation[0])}
    def step(self, period):
        result=super().step(period)
        if result==-1: return result
        if self.start is None: self.start=self.pose()
        self.tick+=1
        if self.tick > PHASES[self.phase][2]:
            name=PHASES[self.phase][0]
            self.results[name]={'start':self.start,'end':self.pose()}
            self.phase+=1
            self.tick=0
            if self.phase==len(PHASES):
                r=self.results
                dx=lambda key:r[key]['end']['position'][0]-r[key]['start']['position'][0]
                yaw=lambda key:r[key]['end']['yaw']-r[key]['start']['yaw']
                checks={
                    'W_forward':dx('forward')>.10,
                    'S_backward':dx('backward')<-.10,
                    'A_turn_left':yaw('left')>.5,
                    'D_turn_right':yaw('right')<-.5,
                    'ground_stable':all(-.002<v['end']['position'][2]<.005 for v in r.values()),
                    'wall_blocks_robot':11.80<r['wall']['end']['position'][0]<11.88,
                    'wall_approach_moved':dx('wall')>.1,
                }
                Path(REPORT).write_text(json.dumps({'checks':checks,'measurements':r,
                    'method':'Unmodified original controller via runpy; scripted Keyboard test double; actual Webots physics',
                    'manual_keyboard_verified':False},indent=2))
                print('FACILITY_SMOKE_TEST '+json.dumps(checks),flush=True)
                self.simulationQuit(0 if all(checks.values()) else 1)
                return -1
            if PHASES[self.phase][0]=='wall':
                node=self.getSelf()
                node.getField('translation').setSFVec3f([11.65,0,0])
                node.getField('rotation').setSFRotation([0,0,1,0])
                node.resetPhysics()
            # Read after the next physics step so Supervisor teleports have applied.
            self.start=None
        return result

controller.Robot=Probe
runpy.run_path(ORIGINAL,run_name='__main__')
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--webots',default='C:/Program Files/Webots/msys64/mingw64/bin/webots.exe')
    args=parser.parse_args()
    project=Path(tempfile.mkdtemp(prefix='secureguard-smoke-'))
    world_dir=project/'worlds'; world_dir.mkdir()
    controller_dir=project/'controllers/facility_probe'; controller_dir.mkdir(parents=True)
    report=ROOT/'worlds/secureguard_facility.runtime.json'
    source=ROOT/'worlds/secureguard_facility.wbt'
    world=source.read_text()
    world=re.sub(r'"(\.\./protos/[^\"]+|textures/[^\"]+)"',
                 lambda m:'"'+(source.parent/m[1]).resolve().as_posix()+'"',world)
    world=world.replace('controller "secureguard_controller"','controller "facility_probe"\n  supervisor TRUE')
    (world_dir/'smoke.wbt').write_text(world)
    harness=HARNESS.replace('__ORIGINAL__',repr(str(ROOT/'controllers/secureguard_controller/secureguard_controller.py')))
    harness=harness.replace('__REPORT__',repr(str(report)))
    (controller_dir/'facility_probe.py').write_text(harness)
    print('Isolated test project:',project,flush=True)
    command=[args.webots,'--batch','--mode=fast','--no-rendering','--minimize','--stdout','--stderr',
             '--port=1236',str(world_dir/'smoke.wbt')]
    try:
        result=subprocess.run(command,capture_output=True,text=True,timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise SystemExit('Webots smoke test timed out; inspect '+str(project)) from exc
    (ROOT/'worlds/secureguard_facility.runtime.log').write_text(result.stdout+'\n'+result.stderr)
    print(result.stdout)
    print(result.stderr)
    if result.returncode:
        raise SystemExit(result.returncode)
    data=json.loads(report.read_text())
    assert all(data['checks'].values()),data
    data['world_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
    data['controller_sha256']=hashlib.sha256((ROOT/'controllers/secureguard_controller/secureguard_controller.py').read_bytes()).hexdigest()
    data['webots_executable']=args.webots
    report.write_text(json.dumps(data,indent=2)+'\n')
    print('PASS:',json.dumps(data['checks']))


if __name__=='__main__': main()
