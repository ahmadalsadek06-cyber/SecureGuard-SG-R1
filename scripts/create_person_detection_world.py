"""Build worlds/secureguard_person_detection.wbt from secureguard_large_robot.wbt.

The source world is read only. Facility geometry is copied verbatim; SG-R1 gets a
clean spawn, the person-detection controller, supervisor TRUE and a Recognition node.
Three static NPC Solids are appended.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'worlds/secureguard_large_robot.wbt'
OUTPUT = ROOT/'worlds/secureguard_person_detection.wbt'

# name, (x, y, yaw), shirt RGB, recognition colour
NPCS = [
    ('Student_01', (9.5, 0.6, 3.14159), (0.15, 0.45, 0.85), (0.15, 0.45, 0.85)),   # Central Corridor
    ('Employee_01', (6.5, -3.5, 1.5708), (0.10, 0.60, 0.25), (0.10, 0.60, 0.25)),  # Warehouse
    ('Visitor_01', (6.8, 3.0, -1.5708), (0.90, 0.45, 0.05), (0.90, 0.45, 0.05)),   # Server Room
]

ROBOT = '''DEF SG_R1 SecureGuardSGR1 {
  translation 5 0 0.005
  name "SecureGuard SG-R1"
  controller "sgr1_person_detection"
  supervisor TRUE
  cameraRecognition Recognition {
    maxRange 10
    occlusion 1
    frameColor 1 0.1 0.1
    frameThickness 2
  }
}
'''


def npc(name, pose, shirt, colour):
    x, y, yaw = pose
    c = ' '.join(f'{v:g}' for v in shirt)
    r = ' '.join(f'{v:g}' for v in colour)
    skin = 'PBRAppearance { baseColor 0.85 0.68 0.55 roughness 0.8 metalness 0 }'
    cloth = f'PBRAppearance {{ baseColor {c} roughness 0.8 metalness 0 }}'
    pants = 'PBRAppearance { baseColor 0.12 0.12 0.16 roughness 0.8 metalness 0 }'
    return f'''DEF {name.upper()} Solid {{
  translation {x} {y} 0
  rotation 0 0 1 {yaw}
  name "{name}"
  model "{name}"
  recognitionColors [ {r} ]
  children [
    Pose {{ translation 0 -0.1 0.42 children [ Shape {{ appearance {pants} geometry Box {{ size 0.16 0.14 0.84 }} }} ] }}
    Pose {{ translation 0 0.1 0.42 children [ Shape {{ appearance {pants} geometry Box {{ size 0.16 0.14 0.84 }} }} ] }}
    Pose {{ translation 0 0 1.15 children [ Shape {{ appearance {cloth} geometry Box {{ size 0.26 0.44 0.62 }} }} ] }}
    Pose {{ translation 0 0 1.6 children [ Shape {{ appearance {skin} geometry Sphere {{ radius 0.12 subdivision 2 }} }} ] }}
    Pose {{ translation 0.04 0 1.24 children [ Shape {{ appearance PBRAppearance {{ baseColor 1 1 1 roughness 0.6 metalness 0 }} geometry Box {{ size 0.2 0.12 0.08 }} }} ] }}
  ]
  boundingObject Pose {{ translation 0 0 0.86 children [ Cylinder {{ radius 0.25 height 1.72 }} ] }}
}}
'''


def build():
    text = SOURCE.read_text()
    start = text.index('DEF SG_R1 SecureGuardSGR1 {')
    end = text.index('\n}\n', start) + 3
    facility = text[:start]
    facility = re.sub(r'title "[^"]*"', 'title "SecureGuard SG-R1 Person Detection"', facility, count=1)
    # Saved source viewpoint can be stale; start behind the robot looking down the corridor.
    lines = facility.split(chr(10))
    v = lines.index('Viewpoint {')
    keep = [l for l in lines[v+1:v+4] if not l.strip().split(' ')[0] in ('fieldOfView', 'orientation', 'position')]
    lines[v+1:v+4] = ['  fieldOfView 0.9', '  orientation 0 1 0 0.3', '  position 2.2 0 2.0'] + keep
    facility = chr(10).join(lines)
    body = facility + ROBOT + ''.join(npc(*n) for n in NPCS) + text[end:]
    OUTPUT.write_text(body)
    return OUTPUT


if __name__ == '__main__':
    print('Wrote', build())
