"""Isolated real-physics validation of the delivered SG-R1 controller and facility."""
from pathlib import Path
from collections import deque
import argparse
import hashlib
import json
import math
import re
import subprocess
import tempfile
import generate_facility as layout

ROOT=Path(__file__).resolve().parents[1]
WORLD=ROOT/'worlds/secureguard_large_robot.wbt'
CONTROLLER=ROOT/'controllers/sgr1_controller/sgr1_controller.py'
REPORT=ROOT/'worlds/secureguard_large_robot.runtime.json'


def check_layout():
    source=(ROOT/'worlds/secureguard_facility.wbt').read_text()
    world=WORLD.read_text()
    a=source[source.index('DEF FOUNDATION'):source.index('\nE-puck {')]
    b=world[world.index('DEF FOUNDATION'):world.index('\nDEF SG_R1')]
    assert a==b, 'Facility geometry was changed'
    obstacles=[x for x in layout.Facility().build().colliders if x['kind']!='floor']
    # A circumscribed .43 m radius + .03 m margin permits rotation in place.
    # Disc-vs-AABB test avoids falsely blocking tight corners with square inflation.
    radius=math.hypot(.70,.50)/2+.03
    def free(x,y):
        return not any(math.hypot(max(abs(x-c['center'][0])-c['size'][0]/2,0),
                                  max(abs(y-c['center'][1])-c['size'][1]/2,0))<radius for c in obstacles)
    start=(5,0); seen={start}; pending=deque([start])
    while pending:
        x,y=pending.popleft()
        for xx,yy in ((x+1,y),(x-1,y),(x,y+1),(x,y-1)):
            if -115<=xx<=115 and -85<=yy<=75 and (xx,yy) not in seen and free(xx/10,yy/10):
                seen.add((xx,yy)); pending.append((xx,yy))
    reachable={code:(round(target[0]*10),round(target[1]*10)) in seen for code,_,_,target,_ in layout.AREAS}
    assert all(reachable.values()),reachable
    assert free(.4631185374,0)
    assert (0,-85) in seen, 'External entrance inaccessible'
    result={'facility_geometry_unchanged':True,'geometry_sha256':hashlib.sha256(a.encode()).hexdigest(),
            'conservative_rotating_clearance_radius_m':radius,'reachable_areas':reachable,
            'minimum_door_width_m':1.8,'robot_width_m':.50,'clearance_each_side_at_narrowest_door_m':.65,
            'corridor_clear_width_m':2.8,'wheelbase_m':.43,'track_m':.43,
            'note':'Door-to-zone routes checked; this does not promise access behind every piece of furniture.'}
    (ROOT/'worlds/secureguard_large_robot.clearance.json').write_text(json.dumps(result,indent=2))
    return result


HARNESS=r'''
import controller
import runpy
import math
import json
from pathlib import Path
ORIGINAL=__ORIGINAL__
REPORT=__REPORT__
# duration, held key, optional [x,y,yaw] reset between independent scenarios
PHASES=[('settle',2,None,None),('forward',4,'W',None),('stop',2,None,None),
        ('backward',4,'S',None),('stop_reverse',2,None,None),
        ('left',2,'A',None),('stop_left',1,None,None),
        ('right',2,'D',None),('stop_right',1,None,None),
        ('brake_approach',2,'W',[0,0,0]),('space_brake',2,' ',None),
        ('wall',3,'W',[11.1,0,0]),('wall_stop',1,None,None),
        ('A1',3,'W',[-8,0,-math.pi/2]),('A2',3,'W',[0,0,-math.pi/2]),
        ('B2',3,'W',[8,0,-math.pi/2]),('C1',3,'W',[-6,0,math.pi/2]),
        ('C2',3,'W',[6,0,math.pi/2]),('external_entry',3,'W',[0,-5.6,-math.pi/2]),
        ('B1',20,'W',[-10,0,0]),
        ('camera',1,'C',[0,0,0]),('final_stop',2,None,None)]

class Keys:
    def __init__(self,owner): self.owner=owner
    def enable(self,period): pass
    def getKey(self):
        if self.owner.consumed: return -1
        self.owner.consumed=True
        key=PHASES[self.owner.phase][2]
        return ord(key) if key else -1

class Probe(controller.Supervisor):
    def __init__(self):
        super().__init__()
        self.phase=0; self.elapsed=0; self.consumed=False
        self.samples=[]; self.results={}; self.reset_wait=False
    def getKeyboard(self): return Keys(self)
    def sample(self):
        n=self.getSelf(); o=n.getOrientation()
        return dict(time=self.getTime(),position=n.getPosition(),velocity=n.getVelocity(),
                    yaw=math.atan2(o[3],o[0]),tilt=math.acos(max(-1,min(1,o[8]))))
    def step(self,period):
        status=super().step(period)
        if status==-1:return -1
        self.consumed=False
        self.elapsed+=period/1000
        self.samples.append(self.sample())
        name,duration,key,_=PHASES[self.phase]
        if self.elapsed>=duration:
            samples=self.samples
            yaw=sum(math.atan2(math.sin(b['yaw']-a['yaw']),math.cos(b['yaw']-a['yaw'])) for a,b in zip(samples,samples[1:]))
            cruise=samples[len(samples)//2:]
            speeds=[math.hypot(s['velocity'][0],s['velocity'][1]) for s in cruise]
            self.results[name]={'start':samples[0],'end':samples[-1],
                'max_tilt_rad':max(s['tilt'] for s in samples),
                'min_z':min(s['position'][2] for s in samples),
                'max_z':max(s['position'][2] for s in samples),
                'cruise_speed_m_s':sum(speeds)/len(speeds),
                'yaw_change_rad':yaw}
            if name=='camera':
                image=self.getDevice('front_camera').getImage()
                self.results[name]['image_bytes']=len(image) if image else 0
                self.results[name]['distinct_sampled_colors']=len({image[i:i+3] for i in range(0,len(image),40)}) if image else 0
                self.results[name]['distances_m']={d:self.getDevice('distance_'+d).getValue()
                    for d in ('front','rear','left','right','front_left','front_right')}
            self.phase+=1
            if self.phase==len(PHASES):
                r=self.results
                speed=lambda n:math.hypot(*r[n]['end']['velocity'][:2])
                dx=lambda n:r[n]['end']['position'][0]-r[n]['start']['position'][0]
                checks={
                    'forward_speed_0_8_to_1_2':.8<=r['forward']['cruise_speed_m_s']<=1.2,
                    'backward_speed_0_8_to_1_2':.8<=r['backward']['cruise_speed_m_s']<=1.2,
                    'forward_direction':dx('forward')>2,
                    'backward_direction':dx('backward')< -2,
                    'left_turn':r['left']['yaw_change_rad']>.5,
                    'right_turn':r['right']['yaw_change_rad']<-.5,
                    'release_stops':speed('stop')<.02 and speed('stop_reverse')<.02,
                    'space_stops':speed('space_brake')<.02,
                    'wall_blocks_body':11.50<r['wall']['end']['position'][0]<11.58,
                    'wall_stops_translation':speed('wall')<.03,
                    'stable_upright':max(v['max_tilt_rad'] for v in r.values())<.20,
                    'stable_on_floor':all(-.02<v['min_z'] and v['max_z']<.04 for v in r.values()),
                    'corridor_traversal':dx('B1')>17 and abs(r['B1']['end']['position'][1])<.2,
                    'camera_image':r['camera']['image_bytes']==160*120*4,
                    'camera_sees_scene':r['camera']['distinct_sampled_colors']>3,
                    'distance_sensors':all(math.isfinite(v) and 0<=v<=5.001 for v in r['camera']['distances_m'].values()),
                }
                checks['external_entrance']=r['external_entry']['end']['position'][1]<-8.25
                for name in ('A1','A2','B2'):checks['door_'+name]=r[name]['end']['position'][1]<-2
                for name in ('C1','C2'):checks['door_'+name]=r[name]['end']['position'][1]>2
                Path(REPORT).write_text(json.dumps({'checks':checks,'measurements':r,
                    'method':'Actual Webots physics and delivered SG-R1 controller; injected held keys in isolated Supervisor test copy',
                    'manual_keyboard_verified':False},indent=2))
                self.simulationQuit(0 if all(checks.values()) else 1)
                return -1
            self.elapsed=0;self.samples=[]
            reset=PHASES[self.phase][3]
            if reset:
                n=self.getSelf()
                n.getField('translation').setSFVec3f([reset[0],reset[1],.005])
                n.getField('rotation').setSFRotation([0,0,1,reset[2]])
                n.resetPhysics()
        return status

controller.Robot=Probe
runpy.run_path(ORIGINAL,run_name='__main__')
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--webots',default='C:/Program Files/Webots/msys64/mingw64/bin/webots.exe')
    args=parser.parse_args()
    print(json.dumps(check_layout(),indent=2),flush=True)
    folder=Path(tempfile.mkdtemp(prefix='secureguard-sgr1-'))
    (folder/'worlds').mkdir();(folder/'controllers/sgr1_probe').mkdir(parents=True)
    text=WORLD.read_text()
    text=re.sub(r'"(\.\./protos/[^\"]+|textures/[^\"]+)"',lambda m:'"'+(WORLD.parent/m[1]).resolve().as_posix()+'"',text)
    text=text.replace('controller "sgr1_controller"','controller "sgr1_probe"\n  supervisor TRUE')
    world=folder/'worlds/test.wbt';world.write_text(text)
    harness=HARNESS.replace('__ORIGINAL__',repr(str(CONTROLLER))).replace('__REPORT__',repr(str(REPORT)))
    (folder/'controllers/sgr1_probe/sgr1_probe.py').write_text(harness)
    print('Test project:',folder,flush=True)
    result=subprocess.run([args.webots,'--batch','--mode=fast','--no-rendering','--minimize','--stdout','--stderr',
                           '--port=1240',str(world)],capture_output=True,text=True,timeout=180)
    (ROOT/'worlds/secureguard_large_robot.runtime.log').write_text(result.stdout+'\n'+result.stderr)
    print(result.stdout+result.stderr)
    data=json.loads(REPORT.read_text())
    data['sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (WORLD,CONTROLLER,ROOT/'protos/SecureGuardSGR1.proto')}
    REPORT.write_text(json.dumps(data,indent=2)+'\n')
    print(json.dumps(data['checks'],indent=2))
    print('Measured forward speed:',data['measurements']['forward']['cruise_speed_m_s'])
    assert result.returncode==0 and all(data['checks'].values()),'Runtime checks failed; inspect report'


if __name__=='__main__':main()
