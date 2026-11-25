"""Build the SG-R1 PROTO and a separate world from the existing facility.

Only the robot, EXTERNPROTO and WorldInfo robot-contact settings change.
The original E-puck world and controller are never written.
"""
from pathlib import Path
from datetime import datetime
import hashlib
import json
import math
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'worlds/secureguard_facility.wbt'
TARGET = ROOT/'worlds/secureguard_large_robot.wbt'
PROTO = ROOT/'protos/SecureGuardSGR1.proto'
COLORS = {'shell':'.80 .85 .88', 'dark':'.06 .09 .12', 'rubber':'.035 .04 .045',
          'blue':'.02 .40 .60', 'light':'.65 .90 1', 'red':'.65 .06 .05', 'metal':'.3 .35 .39'}


def box(x,y,z,sx,sy,sz,color):
    return f'''Pose {{ translation {x} {y} {z} children [ Shape {{
      appearance PBRAppearance {{ baseColor {COLORS[color]} roughness .8 metalness 0 }}
      geometry Box {{ size {sx} {sy} {sz} }} }} ] }}'''


def collision(x,y,z,sx,sy,sz):
    return f'Pose {{ translation {x} {y} {z} children [ Box {{ size {sx} {sy} {sz} }} ] }}'


def chamfered_shell(x,y,z,sx,sy,sz):
    # Eight-sided extrusion: 16 vertices, no imported mesh or subdivision surface.
    a,b,c=sx/2,sy/2,min(sx,sy)*.10
    outline=[(-a+c,-b),(a-c,-b),(a,-b+c),(a,b-c),(a-c,b),(-a+c,b),(-a,b-c),(-a,-b+c)]
    points=[(px,py,pz) for pz in (-sz/2,sz/2) for px,py in outline]
    faces=[list(reversed(range(8))),list(range(8,16))]
    faces += [[i,(i+1)%8,(i+1)%8+8,i+8] for i in range(8)]
    return f'''Pose {{ translation {x} {y} {z} children [ Shape {{
      appearance PBRAppearance {{ baseColor {COLORS['shell']} roughness .8 metalness 0 }}
      geometry IndexedFaceSet {{ coord Coordinate {{ point [
        {', '.join(' '.join(str(v) for v in p) for p in points)}
      ] }} coordIndex [ {', '.join(' '.join(str(v) for v in face)+' -1' for face in faces)} ] }}
    }} ] }}'''


def badge_texture():
    path=ROOT/'protos/textures/sgr1_badge.png'
    if path.exists(): return
    from PIL import Image,ImageDraw,ImageFont
    image=Image.new('RGB',(512,128),(15,25,34));draw=ImageDraw.Draw(image)
    font=Path('C:/Windows/Fonts/arialbd.ttf')
    def face(size):return ImageFont.truetype(str(font),size) if font.exists() else ImageFont.load_default(size=size)
    draw.rectangle((0,0,9,128),fill=(20,170,204))
    draw.text((26,10),'SECUREGUARD',font=face(34),fill=(167,214,231))
    draw.text((26,59),'SG-R1 / PATROL',font=face(42),fill='white')
    path.parent.mkdir(parents=True,exist_ok=True);image.save(path)


def make_proto():
    shapes=[box(0,0,.30,.62,.35,.22,'dark'),
            chamfered_shell(0,0,.445,.53,.37,.11),
            box(-.04,0,.61,.20,.24,.22,'shell'),
            chamfered_shell(.015,0,.79,.29,.32,.18),
            box(.164,0,.79,.01,.28,.105,'dark'),
            box(-.135,0,.79,.012,.23,.09,'dark'),
            box(0,0,.89,.22,.22,.02,'dark')]
    for y in (-.19,.19):
        shapes += [box(0,y,.445,.49,.012,.035,'blue'),
                   box(-.04,y*.64,.61,.15,.008,.04,'blue')]
    for x in (-.33,.33):
        shapes.append(box(x,0,.29,.04,.40,.10,'rubber'))
    for y in (-.13,.13):
        shapes.append(box(.352,y,.325,.006,.07,.025,'light'))
        shapes.append(box(-.352,y,.325,.006,.06,.025,'red'))
    shapes.append(box(.02,0,.885,.17,.16,.01,'blue'))
    for x,y,z,rotation,width,height in [(.266,0,.445,'1 1 1 2.09439510239',.27,.0675),
                                       (0,-.191,.445,'1 0 0 1.57079632679',.30,.075)]:
        shapes.append(f'''Pose {{ translation {x} {y} {z} rotation {rotation} children [ Shape {{
          appearance PBRAppearance {{ baseColorMap ImageTexture {{ url [ "textures/sgr1_badge.png" ] }}
          roughness 1 metalness 0 }} geometry Plane {{ size {width} {height} }} }} ] }}''')
    for x in (-.215,.215):
        shapes.append(f'''Pose {{ translation {x} 0 .13 rotation 1 0 0 1.57079632679
          children [ Shape {{ appearance PBRAppearance {{ baseColor .20 .24 .27 roughness 1 metalness 0 }}
          geometry Cylinder {{ radius .022 height .43 subdivision 12 }} }} ] }}''')
    for axle,x in (('front',.215),('rear',-.215)):
        for side,y in (('left',.215),('right',-.215)):
            name=f'{axle}_{side}'
            shapes.append(f'''HingeJoint {{
      jointParameters HingeJointParameters {{ axis 0 1 0 anchor {x} {y} .13 dampingConstant .015 }}
      device [
        RotationalMotor {{ name "{name}_motor" maxVelocity 12 maxTorque 8 acceleration 20 }}
        PositionSensor {{ name "{name}_encoder" }}
      ]
      endPoint Solid {{
        translation {x} {y} .13
        rotation 1 0 0 1.57079632679
        name "{name}_wheel"
        contactMaterial "SGR1_tire"
        children [
          Shape {{ appearance PBRAppearance {{ baseColor .035 .04 .045 roughness 1 metalness 0 }}
            geometry Cylinder {{ radius .13 height .07 subdivision 20 }} }}
          Shape {{ appearance PBRAppearance {{ baseColor .32 .38 .43 roughness .8 metalness 0 }}
            geometry Cylinder {{ radius .065 height .072 subdivision 12 }} }}
        ]
        boundingObject Cylinder {{ radius .13 height .07 subdivision 20 }}
        physics Physics {{ density -1 mass 1.2 }}
      }}
    }}''')
    shapes.append('''Camera {
      translation .174 0 .80
      name "front_camera"
      fieldOfView 1.1 width 160 height 120 near .03 far 20
      antiAliasing FALSE noise 0 motionBlur 0
      recognition IS cameraRecognition
      children [ Shape { appearance PBRAppearance { baseColor .02 .10 .18 roughness .7 metalness 0 }
        geometry Box { size .012 .055 .04 } } ]
    }
    # Future LiDAR mount: centered at roof, +X forward / +Z up. No scanner yet.
    DEF LIDAR_MOUNT Pose { translation 0 0 .90 children IS lidarSlot }
    InertialUnit { name "imu" }
''')
    for name,x,y,angle in [('front',.356,0,0),('rear',-.356,0,math.pi),
                            ('left',0,.255,math.pi/2),('right',0,-.255,-math.pi/2),
                            ('front_left',.35,.17,.6),('front_right',.35,-.17,-.6)]:
        shapes.append(f'''DistanceSensor {{ translation {x} {y} .32 rotation 0 0 1 {angle}
          name "distance_{name}" type "generic" numberOfRays 1
          lookupTable [ 0 0 0, 5 5 0 ] }}''')
    bo=[collision(0,0,.30,.70,.40,.22),collision(0,0,.445,.53,.38,.11),
        collision(-.04,0,.61,.20,.24,.22),collision(.015,0,.79,.30,.32,.18),
        collision(0,0,.89,.22,.22,.02)]
    return '''#VRML_SIM R2025a utf8
# SecureGuard SG-R1: lightweight four-wheel skid-steer security patrol robot.
# ENU: +X forward, +Y left, +Z up. Floor is robot-local Z=0.
# Envelope approximately .70 x .50 x .90 m; mass 26.8 kg.
# Requires SGR1_tire/default ContactProperties (provided in generated world).
PROTO SecureGuardSGR1 [
  field SFVec3f translation 0 0 .005
  field SFRotation rotation 0 0 1 0
  field SFString name "SecureGuard SG-R1"
  field SFString controller "sgr1_controller"
  field MFString controllerArgs []
  field SFBool supervisor FALSE
  field MFNode lidarSlot []
  field SFNode cameraRecognition NULL  # optional Recognition node; NULL keeps original camera
] {
  Robot {
    translation IS translation rotation IS rotation name IS name
    controller IS controller controllerArgs IS controllerArgs supervisor IS supervisor
    model "SecureGuard SG-R1" window "<generic>"
    contactMaterial "SGR1_body" selfCollision FALSE
    children [
'''+ '\n'.join(shapes)+'''
    ]
    boundingObject Group { children [
'''+ '\n'.join(bo)+'''
    ] }
    physics Physics { density -1 mass 22 centerOfMass [ 0 0 .27 ] }
  }
}
'''


def main():
    original=SOURCE.read_bytes()
    source=original.decode('utf-8-sig').replace('\r\n','\n')
    # ENU is the R2025a default; Webots omits the field when it saves a world from the GUI.
    system=re.search(r'coordinateSystem\s+"([^"]+)"',source)
    assert system is None or system[1]=='ENU', 'Expected inspected Z-up ENU world'
    backup=ROOT/'backups'/('sgr1-build-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    manifest={}
    for path in [SOURCE,ROOT/'worlds/secureguard_world.wbt',ROOT/'controllers/secureguard_controller/secureguard_controller.py',
                 TARGET,PROTO,ROOT/'controllers/sgr1_controller/sgr1_controller.py']:
        if path.exists():
            destination=backup/path.relative_to(ROOT)
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,destination)
            manifest[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    (backup/'sha256.json').write_text(json.dumps(manifest,indent=2))
    # Replace only the original E-puck instance, not any facility geometry.
    match=re.search(r'(?m)^E-puck\s*\{[\s\S]*?^\}',source)
    assert match and not source[match.end():].strip(), 'Expected one terminal E-puck instance'
    world=source[:match.start()]+'''DEF SG_R1 SecureGuardSGR1 {
  translation .4631185374 0 .005
  rotation 0 0 1 0
  controller "sgr1_controller"
}
'''
    world=re.sub(r'EXTERNPROTO "[^"\n]*E-puck.proto"','EXTERNPROTO "../protos/SecureGuardSGR1.proto"',world)
    # 16 ms physics step for SG-R1. A GUI-saved source omits the default 32, so insert it if absent.
    if re.search(r'basicTimeStep\s+\d+',world):
        world=re.sub(r'basicTimeStep\s+\d+','basicTimeStep 16',world,count=1)
    else:
        world=re.sub(r'(?m)^(WorldInfo \{\n  title [^\n]*\n)',r'\1  basicTimeStep 16\n',world,count=1)
    assert 'basicTimeStep 16' in world
    world=world.replace('title "SecureGuard | University Security Facility"','title "SecureGuard | SG-R1 Patrol Facility"')
    # Start inside the corridor so the new robot is visible instead of hidden by walls.
    yaw=math.atan2(.9, .4631185374-2.7)
    pitch=math.atan2(.9,math.hypot(.9,.4631185374-2.7))
    qw=math.cos(yaw/2)*math.cos(pitch/2)
    q=(-math.sin(yaw/2)*math.sin(pitch/2),math.cos(yaw/2)*math.sin(pitch/2),
       math.sin(yaw/2)*math.cos(pitch/2))
    axis=[value/math.sqrt(1-qw*qw) for value in q]
    view=f'''Viewpoint {{
  orientation {' '.join(str(v) for v in axis)} {2*math.acos(qw)}
  position 2.7 -.9 1.35
  fieldOfView .9 near .02 ambientOcclusionRadius 0
  follow "SecureGuard SG-R1" followType "Tracking Shot"
}}'''
    world=re.sub(r'(?m)^Viewpoint\s*\{[\s\S]*?^\}',lambda _:view,world)
    contact='''
  contactProperties [
    ContactProperties {
      material1 "SGR1_tire" material2 "default"
      coulombFriction [ .8 .25 ] forceDependentSlip [ .0005 .005 ]
      bounce 0 softERP .3 softCFM .0001
      bumpSound "" rollSound "" slideSound "" maxContactJoints 6
    }
    ContactProperties {
      material1 "SGR1_body" material2 "default"
      coulombFriction .35 bounce 0 softERP .3 softCFM .0001
      bumpSound "" rollSound "" slideSound "" maxContactJoints 6
    }
  ]
'''
    world=world.replace('  FPS 30\n}', '  FPS 30\n'+contact+'}')
    assert 'SGR1_tire' in world
    # The facility section, including every collision boundary, remains byte-for-byte identical as text.
    original_geometry=source[source.index('DEF FOUNDATION'):match.start()]
    new_geometry=world[world.index('DEF FOUNDATION'):world.index('DEF SG_R1')]
    assert original_geometry==new_geometry
    badge_texture()
    PROTO.write_text(make_proto(),encoding='utf-8')
    TARGET.write_text(world,encoding='utf-8')
    assert SOURCE.read_bytes()==original
    print('Created',PROTO,'and',TARGET,'\nBackup:',backup)
    print('Facility geometry unchanged:',hashlib.sha256(original_geometry.encode()).hexdigest())


if __name__=='__main__': main()
