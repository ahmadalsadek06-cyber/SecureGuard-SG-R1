#!/usr/bin/env python3
"""Generate the SecureGuard facility. Python 3.9+; Pillow only for rebuilding labels.

All geometry uses ENU (X east, Y north, Z up). The source robot is copied verbatim.
Default operation is offline when the bundled assets and labels are present.
"""
from pathlib import Path
from collections import deque, Counter
from datetime import datetime
import argparse
import hashlib
import json
import math
import os
import posixpath
import re
import shutil
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'worlds/secureguard_world.wbt'
CONTROLLER = ROOT / 'controllers/secureguard_controller/secureguard_controller.py'
OUTPUT = ROOT / 'worlds/secureguard_facility.wbt'
VENDOR = ROOT / 'protos/vendor'
ROBOT_ASSET = 'projects/robots/gctronic/e-puck/protos/E-puck.proto'
BASE = 'https://raw.githubusercontent.com/cyberbotics/webots/R2025a/'
COLORS = {'wall': (.78,.82,.86), 'trim': (.10,.20,.29),
          'wood': (.55,.36,.19), 'steel': (.20,.27,.32),
          'teal': (.03,.48,.51), 'white': (.82,.88,.89),
          'blue': (.15,.35,.60), 'amber': (.85,.48,.13)}
AREAS = [
    ('A1', 'Reception', (-11.9,-4.1,-7.9,-1.6), (-8,-3), 'teal'),
    ('A2', 'Main Entrance', (-3.9,3.9,-7.9,-1.6), (0,-4), 'amber'),
    ('B1', 'Central Corridor', (-11.9,11.9,-1.4,1.4), (5,0), 'blue'),
    ('B2', 'Warehouse', (4.1,11.9,-7.9,-1.6), (8,-3), 'amber'),
    ('C1', 'Research Laboratory', (-11.9,-.1,1.6,7.9), (-6,3), 'teal'),
    ('C2', 'Server Room', (.1,11.9,1.6,7.9), (6,3), 'blue'),
]


def backup():
    directory = ROOT / 'backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    directory.mkdir(parents=True)
    files = [SOURCE, CONTROLLER]
    if OUTPUT.exists():
        files.append(OUTPUT)
    manifest = {}
    for path in files:
        shutil.copy2(path, directory / path.name)
        manifest[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    (directory / 'sha256.json').write_text(json.dumps(manifest, indent=2))
    return str(directory.relative_to(ROOT))


def bundle_assets():
    """Vendor the exact R2025a robot and its recursive assets; retain license headers."""
    visited = set()
    def fetch(relative):
        relative = posixpath.normpath(relative)
        if relative.startswith('../') or relative in visited:
            if relative.startswith('../'):
                raise ValueError('Asset path escapes vendor directory')
            return
        visited.add(relative)
        target = VENDOR / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            print('Bundling', relative, flush=True)
            with urllib.request.urlopen(BASE + relative, timeout=45) as response:
                target.write_bytes(response.read())
        if target.suffix != '.proto':
            return
        source = target.read_text(encoding='utf-8')
        # Resource strings, including inactive robot-version branches.
        pattern = r'([\"\'])([^\"\'\n]+\.(?:proto|png|jpg|jpeg|obj|stl|dae))\1'
        def resource(match):
            value = match[2]
            if value.startswith('webots://'):
                dependency = value[9:]
            elif value.startswith(BASE):
                dependency = value[len(BASE):]
            elif '://' in value:
                raise ValueError('Unexpected remote asset: ' + value)
            else:
                dependency = posixpath.normpath(posixpath.join(posixpath.dirname(relative), value))
            fetch(dependency)
            return match[1] + posixpath.relpath(dependency, posixpath.dirname(relative)) + match[1]
        # Ignore documentation/license comments when finding dependencies.
        source = '\n'.join(line if line.lstrip().startswith('#') else re.sub(pattern, resource, line)
                           for line in source.splitlines()) + '\n'
        if relative == ROBOT_ASSET:
            # Use Webots' built-in inspector instead of optional platform-specific
            # e-puck robot-window/Bluetooth plugins. Robot physics/devices unchanged.
            source = re.sub(r'(field\s+SFString\s+window\s+)"e-puck"',
                            r'\1"<generic>"', source)
        target.write_text(source, encoding='utf-8')
    fetch(ROBOT_ASSET)
    return len(visited)


def labels(rebuild=False):
    directory = ROOT / 'worlds/textures/facility'
    directory.mkdir(parents=True, exist_ok=True)
    for code, name, _, _, color in AREAS:
        path = directory / (code + '.png')
        if path.exists() and not rebuild:
            continue
        from PIL import Image, ImageDraw, ImageFont
        im = Image.new('RGB', (1024,256), (22,38,51))
        draw = ImageDraw.Draw(im)
        rgb = tuple(int(v*255) for v in COLORS[color])
        draw.rectangle((0,0,18,256), fill=rgb)
        font_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/arialbd.ttf'
        def font(size):
            return ImageFont.truetype(str(font_path), size) if font_path.exists() else ImageFont.load_default(size=size)
        draw.text((45,22), code + '  /  SECUREGUARD', font=font(39), fill=(111,201,205))
        draw.text((45,95), name.upper(), font=font(61 if len(name)<18 else 48), fill='white')
        draw.text((45,197), 'UNIVERSITY SECURITY FACILITY', font=font(25), fill=(169,185,198))
        im.save(path)


def extract_node(source, node):
    match = re.search(r'(?m)^' + re.escape(node) + r'\s*\{', source)
    if not match:
        raise ValueError('Source must contain ' + node)
    depth = 1
    i = match.end()
    while depth:
        if source[i] == '{': depth += 1
        if source[i] == '}': depth -= 1
        i += 1
    return source[match.start():i]


def vec(values):
    return ' '.join(f'{n:.8g}' for n in values)


class Facility:
    def __init__(self):
        self.nodes = []
        self.colliders = []

    def box(self, name, center, size, color, collision=True, kind='obstacle'):
        appearance = f'PBRAppearance {{ baseColor {vec(COLORS.get(color, color))} roughness 1 metalness 0 }}'
        geometry = f'Box {{ size {vec(size)} }}'
        self.nodes.append(f'DEF {name} ' + ('Solid' if collision else 'Pose') + ' {\n'
                          f'  translation {vec(center)}\n' +
                          (f'  name "{name}"\n' if collision else '') +
                          f'  children [ Shape {{ appearance {appearance} geometry {geometry} }} ]\n' +
                          (f'  boundingObject {geometry}\n' if collision else '') + '}\n')
        if collision:
            self.colliders.append(dict(name=name, center=center, size=size, kind=kind))

    def wall(self, name, x, y, sx, sy):
        self.box(name, (x,y,1.25), (sx,sy,2.5), 'wall', kind='wall')
        self.box(name+'_CAP', (x,y,2.515), (sx,sy,.03), 'trim', False)

    def shelf(self, name, x, y):
        # Detailed lightweight visual, one conservative collision box: the robot
        # cannot drive under stored goods or snag on narrow shelf legs.
        self.box(name, (x,y,1), (.85,3.5,2), 'steel')
        self.nodes.pop()
        parts=[]
        for dx in (-.38,.38):
            for dy in (-1.70,1.70):
                parts.append(((dx,dy,0),(.09,.09,2),'steel'))
        for z in (-.88,-.25,.38,.94):
            parts.append(((0,0,z),(.85,3.5,.08),'amber'))
        for j,z in enumerate((-.65,-.02,.61)):
            for dy in (-1.1,0,1.1):
                parts.append(((0,dy,z),(.65,.75,.38),'wood'))
        children=[]
        for center,size,color in parts:
            children.append(f'Pose {{ translation {vec(center)} children [ Shape {{ '
                            f'appearance PBRAppearance {{ baseColor {vec(COLORS[color])} roughness 1 metalness 0 }} '
                            f'geometry Box {{ size {vec(size)} }} }} ] }}')
        self.nodes.append(f'DEF {name} Solid {{ translation {x} {y} 1 name "{name}" '
                          f'children [ {" ".join(children)} ] boundingObject Box {{ size .85 3.5 2 }} }}')

    def horizontal_doors(self, name, y, openings):
        edge = -11.9
        for i, (a,b) in enumerate(openings + [(11.9,11.9)]):
            if a > edge:
                self.wall(name+str(i), (edge+a)/2, y, a-edge, .2)
            edge = b

    def sign(self, code, x, y, width):
        # A horizontal two-triangle sign. No collidable lip, text faces +Z.
        self.nodes.append(f'''DEF LABEL_{code} Pose {{
  translation {x} {y} 0.006
  children [ Shape {{
    appearance PBRAppearance {{
      baseColorMap ImageTexture {{ url [ "textures/facility/{code}.png" ] }}
      roughness 1 metalness 0
    }}
    geometry Plane {{ size {width} {width/4} }}
  }} ]
}}
''')

    def build(self):
        self.box('FOUNDATION', (0,0,-.10), (24.2,16.2,.2), (.48,.55,.60), kind='floor')
        for code, name, bounds, target, color in AREAS:
            x0,x1,y0,y1 = bounds
            shade = {'teal':(.70,.81,.80),'amber':(.85,.80,.68),'blue':(.67,.76,.84)}[color]
            self.box('FLOOR_'+code, ((x0+x1)/2,(y0+y1)/2,.001), (x1-x0,y1-y0,.002), shade, False)
            sx,sy = target
            self.sign(code,sx,sy, 4.8 if code!='B1' else 4.4)
        self.wall('EXT_N',0,8,24.2,.2)
        self.wall('EXT_W',-12,0,.2,15.8)
        self.wall('EXT_E',12,0,.2,15.8)
        self.wall('EXT_S_W',-6.75,-8,10.7,.2)
        self.wall('EXT_S_E',6.75,-8,10.7,.2)
        self.box('ENTRANCE_APRON',(0,-8.7,-.1),(3.2,1.2,.2),'trim',kind='floor')
        self.horizontal_doors('SOUTH_PARTITION_',-1.5,[(-8.9,-7.1),(-1.2,1.2),(7.1,8.9)])
        self.horizontal_doors('NORTH_PARTITION_',1.5,[(-6.9,-5.1),(5.1,6.9)])
        for x in (-4,4): self.wall('SOUTH_DIVIDER_'+str(x).replace('-','M'),x,-4.75,.2,6.3)
        self.wall('NORTH_DIVIDER',0,4.75,.2,6.3)
        # Painted corridor guides: visual only, no raised thresholds.
        for y in (-1.16,1.16):
            self.box('GUIDE_'+str(y).replace('-','M').replace('.','_'),(0,y,.004),(23.5,.045,.002),'teal',False)
        for i,x in enumerate(range(-10,12,2)):
            self.box('DASH_'+str(i),(x,.65,.004),(.55,.045,.002),'white',False)
        # Reception: solid desk, return, seats and monitor.
        self.box('RECEPTION_DESK',(-8,-5.4,.55),(3.6,.9,1.1),'wood')
        self.box('DESK_TOP',(-8,-5.4,1.13),(3.75,1.02,.06),'white',False)
        self.box('DESK_RETURN',(-9.35,-6.25,.55),(.9,.8,1.1),'wood')
        self.box('RECEPTION_MONITOR',(-8.3,-5.4,1.40),(.6,.12,.45),'steel',False)
        for i,x in enumerate((-10.7,-5.3)):
            self.box('WAITING_SEAT_'+str(i),(x,-3.7,.25),(.65,1.2,.5),'teal')
        # Main entrance: broad unobstructed route between two benches.
        for i,x in enumerate((-2.8,2.8)):
            self.box('LOBBY_BENCH_'+str(i),(x,-5.7,.23),(.65,2.5,.46),'wood')
        # Warehouse: three shelving banks, walkable aisles, solid conservative envelope.
        for i,x in enumerate((5.2,8,10.8)):
            self.shelf('SHELF_'+str(i),x,-5.7)
        # Research benches with low-complexity instruments.
        for i,(x,y) in enumerate(((-9,5.3),(-4,5.3))):
            self.box('LAB_BENCH_'+str(i),(x,y,.46),(3.2,1.2,.92),'white')
            self.box('LAB_TOP_'+str(i),(x,y,.96),(3.3,1.3,.08),'teal',False)
            for j,dx in enumerate((-.9,.8)):
                self.box(f'INSTRUMENT_{i}_{j}',(x+dx,y,1.23),(.55,.45,.46),'steel',False)
        self.box('LAB_STORAGE',(-10.8,7.2,.85),(1.4,.8,1.7),'white')
        # Eight server racks in two banks, with 1.5m+ cross aisles.
        for i,x in enumerate((2,4.5,7,9.5)):
            for j,y in enumerate((4.6,6.7)):
                self.box(f'RACK_{i}_{j}',(x,y,1.05),(.85,1.0,2.1),'steel')
                for k,z in enumerate((.35,.7,1.05,1.4,1.75)):
                    self.box(f'RACK_STRIPE_{i}_{j}_{k}',(x,y-.506,z),(.7,.012,.025),'blue',False)
                    self.box(f'RACK_LED_{i}_{j}_{k}',(x+.27,y-.515,z+.06),(.04,.012,.025),'teal',False)
        return self


def validate(world, facility, spawn):
    # Lexical structural checks; Webots itself provides the authoritative parser test.
    clean = re.sub(r'"(?:\\.|[^"\\])*"|#[^\n]*', '', world)
    stack = []
    for char in clean:
        if char in '{[': stack.append(char)
        elif char in '}]':
            assert stack and stack.pop() == {'}':'{',']':'['}[char], 'Unbalanced node structure'
    assert not stack
    defs = re.findall(r'\bDEF\s+(\w+)', clean)
    assert not [n for n,c in Counter(defs).items() if c>1], 'Duplicate DEF'
    assert set(re.findall(r'\bUSE\s+(\w+)', clean)) <= set(defs)
    walls = [c for c in facility.colliders if c['kind']=='wall']
    def overlap(a,b):
        return all(abs(a['center'][i]-b['center'][i]) < (a['size'][i]+b['size'][i])/2-1e-7 for i in range(3))
    for i,a in enumerate(walls):
        for b in walls[i+1:]: assert not overlap(a,b), f'Intersecting walls: {a["name"]}, {b["name"]}'
    obstacles = [c for c in facility.colliders if c['kind']!='floor']
    for obstacle in obstacles:
        assert all(v>0 and math.isfinite(v) for v in obstacle['size'])
        assert abs(obstacle['center'][2]-obstacle['size'][2]/2) < 1e-7, 'Floating collision'
        if obstacle['kind']!='wall':
            for wall in walls: assert not overlap(obstacle,wall), 'Furniture intersects wall'
    # Inflate by 12 cm: wider than the ~3.5 cm E-puck radius, plus grid margin.
    def free(x,y):
        return not any(abs(x-c['center'][0]) <= c['size'][0]/2+.12 and
                       abs(y-c['center'][1]) <= c['size'][1]/2+.12 for c in obstacles)
    assert free(*spawn[:2]), 'Robot spawn blocked'
    step=.1
    def cell(x,y): return round(x/step),round(y/step)
    start=cell(*spawn[:2]); seen={start}; queue=deque([start])
    while queue:
        x,y=queue.popleft()
        for nx,ny in ((x+1,y),(x-1,y),(x,y+1),(x,y-1)):
            if -118<=nx<=118 and -87<=ny<=78 and (nx,ny) not in seen and free(nx*step,ny*step):
                seen.add((nx,ny)); queue.append((nx,ny))
    reachable={code:cell(*target) in seen for code,_,_,target,_ in AREAS}
    assert all(reachable.values()), 'Inaccessible room: '+str(reachable)
    assert cell(0,-8.5) in seen, 'Entrance inaccessible'
    # All free interior cells, including furniture aisles, must connect to spawn.
    disconnected=[(x,y) for x in range(-117,118) for y in range(-77,78)
                  if free(x*step,y*step) and (x,y) not in seen]
    assert not disconnected, f'{len(disconnected)} disconnected free cells'
    for path in re.findall(r'url\s*\[\s*"([^"]+)"',world):
        assert (OUTPUT.parent/path).exists(), 'Missing texture '+path
    return dict(coordinate_system='ENU (X/Y floor, Z up)', footprint_m=[24.2,16.2],
                corridor_clear_width_m=2.8, minimum_room_door_width_m=1.8,
                entrance_width_m=2.8, unique_defs=len(defs), wall_count=len(walls),
                collision_objects=len(facility.colliders), reachable_areas=reachable,
                reachable_grid_cells=len(seen), disconnected_free_cells=0,
                wall_intersections=0, furniture_wall_intersections=0,
                collision_geometry='Local boxes match obstacle extents; shelves use a conservative envelope around posts, decks and crates',
                spawn=list(spawn), spawn_clearance_check_radius_m=.12,
                syntax_check='Balanced structure, unique DEF, resolved USE and textures; see runtime report for Webots parsing')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rebuild-labels',action='store_true')
    args=parser.parse_args()
    original=SOURCE.read_bytes(); controller=CONTROLLER.read_bytes()
    source=original.decode('utf-8-sig')
    info=extract_node(source,'WorldInfo')
    coordinate=re.search(r'coordinateSystem\s+"([^"]+)"',info)
    coordinate=coordinate[1] if coordinate else 'ENU'
    if coordinate!='ENU':
        raise ValueError(f'Source coordinateSystem is {coordinate}; generator expects inspected ENU source. No files changed.')
    robot=extract_node(source,'E-puck')
    assert 'controller "secureguard_controller"' in robot
    spawn=tuple(map(float,re.search(r'(?m)^\s*translation\s+([^\n]+)',robot)[1].split()))
    saved=backup()
    assets=bundle_assets()
    labels(args.rebuild_labels)
    facility=Facility().build()
    # Webots camera looks along local +X, with local +Z up (yaw 135, pitch 39.4 deg).
    world='''#VRML_SIM R2025a utf8
# Generated by scripts/generate_facility.py. Dimensions in meters; no roof for visibility.
EXTERNPROTO "../protos/vendor/projects/robots/gctronic/e-puck/protos/E-puck.proto"
WorldInfo {
  title "SecureGuard | University Security Facility"
  coordinateSystem "ENU"
  basicTimeStep 32
  FPS 30
}
Viewpoint {
  orientation -0.3335380395 0.1381559795 0.9325584708 2.4043492416
  position 25 -25 29
  fieldOfView 0.85
  ambientOcclusionRadius 0
}
Background { skyColor [ 0.12 0.17 0.22 ] }
DirectionalLight { direction -0.4 0.3 -1 intensity 1 ambientIntensity 0.65 castShadows FALSE }
'''+ '\n'.join(facility.nodes)+'\n'+robot+'\n'
    report=validate(world,facility,spawn)
    report.update(backup=saved,bundled_assets=assets,source_robot_copied_verbatim=True,
                  source_sha256=hashlib.sha256(original).hexdigest(),
                  controller_sha256=hashlib.sha256(controller).hexdigest())
    OUTPUT.write_text(world,encoding='utf-8')
    (ROOT/'worlds/secureguard_facility.validation.json').write_text(json.dumps(report,indent=2)+'\n')
    assert SOURCE.read_bytes()==original and CONTROLLER.read_bytes()==controller
    print(json.dumps(report,indent=2))
    print('Generated:',OUTPUT)


if __name__=='__main__':
    main()
