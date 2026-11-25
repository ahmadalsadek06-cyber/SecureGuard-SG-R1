"""Generate a Security Guard Cabin and a new world: worlds/secureguard_with_cabin.wbt.

The cabin is built only from Webots primitives (Box, Cylinder, Plane) inside ONE static
Solid whose boundingObject is a Group of boxes (walls, glass, roof, floor, desk, chair).
It stands OUTSIDE the main entrance on a new collidable exterior plaza, so the six
zones (A1..C2), their furniture, NPCs and authorization rules are untouched.

Source world (read only): worlds/secureguard_person_detection.wbt.
The SG-R1 node is replaced by the clean generated node from
scripts/create_person_detection_world.py (same PROTO, controller, supervisor and
Recognition), and the PROTO reference is made relative so the world is portable.

Coordinates: ENU, metres. Cabin local frame: +X = length, +Y = width (+Y faces the
building), +Z up. Usage examples (PowerShell, from the project folder):

    python scripts\\generate_security_cabin.py                      # defaults
    python scripts\\generate_security_cabin.py --x 4.5 --y -11.5     # move it
    python scripts\\generate_security_cabin.py --yaw-deg 15          # rotate about Z
    python scripts\\generate_security_cabin.py --length 3.2 --width 2.6 --height 2.8
    python scripts\\generate_security_cabin.py --door-width 1.0 --front-window 2.0 \\
        --side-window 1.2 --sill 0.9 --window-top 2.05

Colours: edit the COLORS dictionary below (RGB 0..1).
The script validates placement (no overlap with facility colliders, NPCs or the robot
spawn; walkway in front of the entrance kept clear; cabin fully on the plaza) and
refuses to write the world if a check fails. Existing files are never modified; if the
output world already exists it is backed up first.
"""
from pathlib import Path
from datetime import datetime
import argparse
import hashlib
import json
import math
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import generate_facility as facility          # noqa: E402  (layout + colliders, read only)
import create_person_detection_world as pdw   # noqa: E402  (clean SG-R1 node + NPC list)

SOURCE = ROOT / "worlds/secureguard_person_detection.wbt"
OUTPUT = ROOT / "worlds/secureguard_with_cabin.wbt"
SIGN = ROOT / "worlds/textures/cabin/security_cabin_sign.png"
ROBOT_SPAWN = (5.0, 0.0)               # from create_person_detection_world.ROBOT
ROBOT_RADIUS = math.hypot(0.70, 0.50) / 2 + 0.03   # 0.46 m, same as scripts/test_sgr1.py

COLORS = {                       # professional gray / white / dark blue
    "wall": (0.78, 0.80, 0.82),  # light gray render
    "trim": (0.95, 0.96, 0.97),  # white frames
    "blue": (0.09, 0.17, 0.31),  # dark blue roof, door frame, chair
    "plinth": (0.30, 0.32, 0.35),
    "floor": (0.42, 0.44, 0.47),
    "glass": (0.62, 0.80, 0.92),
    "desk": (0.92, 0.93, 0.94),
    "dark": (0.10, 0.11, 0.13),
    "metal": (0.60, 0.63, 0.67),
    "plaza": (0.56, 0.58, 0.60),
    "walkway": (0.70, 0.72, 0.74),
}

DEFAULTS = dict(x=4.0, y=-11.0, yaw_deg=0.0, length=3.0, width=2.5, height=2.8,
                door_width=0.95, front_window=1.8, side_window=1.0, sill=0.9, window_top=2.05)
WALL_T, ROOF_T, OVERHANG, DOOR_H = 0.12, 0.15, 0.20, 2.10
PLAZA = (-7.0, 7.0, -15.0, -8.1)        # x0, x1, y0, y1 (y1 = foundation edge)
PLAZA_TOP = -0.002                      # 2 mm under the apron/foundation top: no z-fighting
WALKWAY = (-1.4, 1.4)                   # entrance width; kept clear (+ robot radius)


def v(t):
    return " ".join(f"{x:.6g}" for x in t)


def appearance(color, transparency=0.0, emissive=None, roughness=0.8, metalness=0.0):
    extra = f" transparency {transparency:g}" if transparency else ""
    if emissive:
        extra += f" emissiveColor {v(emissive)} emissiveIntensity 1"
    return f"PBRAppearance {{ baseColor {v(COLORS.get(color, color) if isinstance(color, str) else color)} roughness {roughness:g} metalness {metalness:g}{extra} }}"


class Cabin:
    """Collects visual shapes and collision boxes in the cabin's local frame."""

    def __init__(self, a):
        self.a = a
        self.L, self.W, self.H = a.length, a.width, a.height
        self.Hw = self.H - ROOF_T                    # wall height under the roof slab
        self.shapes, self.collide = [], []

    def box(self, center, size, color, collide=True, **look):
        self.shapes.append(f"Pose {{ translation {v(center)} children [ Shape {{ appearance {appearance(color, **look)} "
                           f"geometry Box {{ size {v(size)} }} }} ] }}")
        if collide:
            self.collide.append((center, size))

    def raw(self, text):
        self.shapes.append(text)

    # A straight wall along X (axis='x') or Y (axis='y') with rectangular openings.
    def wall(self, axis, fixed, s0, s1, openings, color="wall"):
        t, Hw = WALL_T, self.Hw
        def put(a0, a1, z0, z1, col=color, collide=True, thick=t, **look):
            if a1 - a0 < 1e-6 or z1 - z0 < 1e-6:
                return
            c = (a0 + a1) / 2
            center = (c, fixed, (z0 + z1) / 2) if axis == "x" else (fixed, c, (z0 + z1) / 2)
            size = (a1 - a0, thick, z1 - z0) if axis == "x" else (thick, a1 - a0, z1 - z0)
            self.box(center, size, col, collide, **look)
        cursor = s0
        for c, w, z0, z1, kind in sorted(openings):
            a0, a1 = c - w / 2, c + w / 2
            assert s0 + 0.05 <= a0 and a1 <= s1 - 0.05, f"opening outside wall span ({kind})"
            put(cursor, a0, 0, Hw)                 # full-height pier
            put(a0, a1, 0, z0)                     # below (sill) - zero for doors
            put(a0, a1, z1, Hw)                    # header above
            if kind == "window":                   # glass pane + white frame
                put(a0, a1, z0, z1, "glass", True, 0.02, transparency=0.55, roughness=0.05)
                for (b0, b1, y0, y1) in ((a0 - 0.05, a0 + 0.01, z0, z1), (a1 - 0.01, a1 + 0.05, z0, z1),   # 1 cm overlap: no z-fighting
                                         (a0 - 0.05, a1 + 0.05, z0 - 0.05, z0 + 0.01), (a0 - 0.05, a1 + 0.05, z1 - 0.01, z1 + 0.05)):
                    put(b0, b1, y0, y1, "trim", False, t + 0.02)
            else:                                  # dark-blue door frame
                for (b0, b1, y0, y1) in ((a0 - 0.06, a0 + 0.01, 0, z1), (a1 - 0.01, a1 + 0.06, 0, z1), (a0 - 0.06, a1 + 0.06, z1 - 0.01, z1 + 0.06)):
                    put(b0, b1, y0, y1, "blue", False, t + 0.02)
            cursor = a1
        put(cursor, s1, 0, Hw)

    def build(self):
        a, L, W, Hw, t = self.a, self.L, self.W, self.Hw, WALL_T
        win = (a.sill, a.window_top)
        door_c = W / 2 - t - 0.15 - a.door_width / 2           # door near the building-side corner
        # North (+Y, faces the building) and South walls run the full length.
        self.wall("x", W / 2 - t / 2, -L / 2, L / 2, [(0.0, a.front_window, *win, "window")])
        self.wall("x", -W / 2 + t / 2, -L / 2, L / 2, [(0.0, a.side_window, *win, "window")])
        # West wall (faces the walkway): entrance doorway + small window. East wall: window.
        inner = (-W / 2 + t, W / 2 - t)
        small = min(0.7, (door_c - a.door_width / 2) - (inner[0] + 0.2))
        self.wall("y", -L / 2 + t / 2, *inner, [(door_c, a.door_width, 0.0, DOOR_H, "door"),
                                               (inner[0] + 0.1 + small / 2 + 0.05, small, *win, "window")])
        self.wall("y", L / 2 - t / 2, *inner, [(0.0, a.side_window, *win, "window")])
        # Exterior plinth band (visual), floor, flat roof with overhang and white fascia.
        for (cx, cy, sx, sy) in ((0, W / 2 + 0.005, L, 0.01), (0, -W / 2 - 0.005, L, 0.01),
                                 (L / 2 + 0.005, 0, 0.01, W), (-L / 2 - 0.005, 0, 0.01, W)):
            self.box((cx, cy, 0.09), (sx, sy, 0.18), "plinth", False)
        self.box((0, 0, 0.02), (L - 2 * t, W - 2 * t, 0.04), "floor")
        self.box((0, 0, Hw + ROOF_T / 2), (L + 2 * OVERHANG, W + 2 * OVERHANG, ROOF_T), "blue")
        self.box((0, 0, Hw + ROOF_T + 0.01), (L + 2 * OVERHANG - 0.1, W + 2 * OVERHANG - 0.1, 0.02), "plinth", False)
        # Security desk under the front window, monitor, keyboard and chair.
        dy = W / 2 - t - 0.32
        self.box((0, dy, 0.375), (1.6, 0.6, 0.75), "desk", collide=True)            # desk body (collision)
        self.box((0, dy, 0.765), (1.66, 0.64, 0.03), "trim", False)                  # top
        self.box((0, dy - 0.31, 0.38), (1.5, 0.01, 0.6), "blue", False)               # front modesty panel
        self.box((0, dy + 0.12, 0.79), (0.24, 0.16, 0.015), "dark", False)            # monitor base
        self.box((0, dy + 0.14, 0.86), (0.05, 0.04, 0.13), "dark", False)             # stand
        self.box((0, dy + 0.14, 1.07), (0.56, 0.04, 0.34), "dark", False)             # screen housing
        self.box((0, dy + 0.118, 1.07), (0.51, 0.005, 0.29), (0.05, 0.12, 0.2), False,
                 emissive=(0.12, 0.32, 0.55), roughness=0.3)                         # lit screen
        self.box((0, dy - 0.12, 0.785), (0.44, 0.14, 0.02), "dark", False)            # keyboard
        cy = dy - 0.72
        self.box((0, cy, 0.47), (0.48, 0.48, 1.0), "blue", collide=True, transparency=1)   # chair collision (invisible)
        self.shapes.pop()
        self.box((0, cy, 0.47), (0.46, 0.46, 0.06), "blue", False)                   # seat
        self.box((0, cy - 0.21, 0.78), (0.44, 0.05, 0.52), "blue", False)             # backrest
        self.raw(f"Pose {{ translation 0 {cy:g} 0.24 children [ Shape {{ appearance {appearance('metal', metalness=0.6, roughness=0.4)} "
                 f"geometry Cylinder {{ radius 0.03 height 0.42 subdivision 10 }} }} ] }}")
        self.raw(f"Pose {{ translation 0 {cy:g} 0.035 children [ Shape {{ appearance {appearance('dark')} "
                 f"geometry Cylinder {{ radius 0.28 height 0.03 subdivision 12 }} }} ] }}")
        # Security camera above the entrance, aimed at the building entrance (decorative housing).
        cam_y = door_c - a.door_width / 2 - 0.25
        self.raw(f"Pose {{ translation {-L / 2 - 0.02:g} {cam_y:g} {Hw - 0.2:g} children [ "
                 f"Shape {{ appearance {appearance('metal')} geometry Box {{ size 0.04 0.12 0.12 }} }} "
                 f"Pose {{ translation -0.08 0 0 children [ Shape {{ appearance {appearance('metal')} geometry Box {{ size 0.16 0.03 0.03 }} }} ] }} "
                 f"Pose {{ translation -0.17 0 -0.02 rotation 0 0 1 -0.6 children [ Pose {{ rotation 0 1 0 -0.35 children [ "
                 f"Pose {{ translation -0.1 0 0 children [ Shape {{ appearance {appearance('trim', roughness=0.4)} geometry Box {{ size 0.26 0.11 0.11 }} }} ] }} "
                 f"Pose {{ translation -0.235 0 0 rotation 0 1 0 1.5708 children [ Shape {{ appearance {appearance('dark', roughness=0.2)} geometry Cylinder {{ radius 0.04 height 0.02 subdivision 12 }} }} ] }} "
                 f"Pose {{ translation -0.05 0 0.07 children [ Shape {{ appearance {appearance('blue')} geometry Box {{ size 0.3 0.15 0.02 }} }} ] }} "
                 f"Pose {{ translation -0.02 0.045 -0.03 children [ Shape {{ appearance {appearance((0.6, 0, 0), emissive=(1, 0, 0))} geometry Sphere {{ radius 0.008 subdivision 1 }} }} ] }} "
                 f"] }} ] }} ] }}")
        # Exterior light fixture next to the door + one PointLight (no shadows: Intel UHD friendly).
        lamp_y = door_c                                                         # centred above the doorway
        self.box((-L / 2 - 0.04, lamp_y, DOOR_H + 0.2), (0.08, 0.28, 0.1), "dark", False)
        self.box((-L / 2 - 0.085, lamp_y, DOOR_H + 0.2), (0.01, 0.24, 0.06), (1, 0.95, 0.8), False,
                 emissive=(1, 0.93, 0.75), roughness=0.3)
        self.raw(f"PointLight {{ location {-L / 2 - 0.6:g} {door_c:g} {DOOR_H + 0.2:g} color 1 0.95 0.85 "
                 f"intensity 0.7 radius 5 castShadows FALSE }}")
        # Sign facing the building (+Y), above the front window: Plane rotated to stand upright.
        self.raw(f"Pose {{ translation 0 {W / 2 + 0.016:g} {(a.window_top + 0.05 + Hw) / 2:g} rotation 0 0 1 3.14159 children [ "
                 f"Pose {{ rotation 1 0 0 1.5708 children [ Shape {{ appearance PBRAppearance {{ baseColorMap ImageTexture "
                 f"{{ url [ \"textures/cabin/security_cabin_sign.png\" ] }} roughness 1 metalness 0 }} "
                 f"geometry Plane {{ size 1.7 0.42 }} }} ] }} ] }}")
        self.raw(f"Pose {{ translation 0 {W / 2 + 0.006:g} {(a.window_top + 0.05 + Hw) / 2:g} children [ Shape {{ "
                 f"appearance {appearance('blue')} geometry Box {{ size 1.8 0.01 0.5 }} }} ] }}")
        return self

    def footprint(self):
        """World-frame corner points of everything that reaches below robot height (walls, camera, lamp)."""
        a = self.a
        ext = 0.45   # camera bracket / lamp protrusion beyond the west wall
        pts = [(-self.L / 2 - ext, -self.W / 2), (self.L / 2, -self.W / 2), (self.L / 2, self.W / 2), (-self.L / 2 - ext, self.W / 2)]
        c, s = math.cos(math.radians(a.yaw_deg)), math.sin(math.radians(a.yaw_deg))
        return [(a.x + px * c - py * s, a.y + px * s + py * c) for px, py in pts]

    def node(self):
        a = self.a
        bo = " ".join(f"Pose {{ translation {v(c)} children [ Box {{ size {v(s)} }} ] }}" for c, s in self.collide)
        body = "\n    ".join(self.shapes)
        return (f"DEF SECURITY_CABIN Solid {{\n  translation {a.x:g} {a.y:g} 0\n  rotation 0 0 1 {math.radians(a.yaw_deg):.6g}\n"
                f"  name \"SECURITY_CABIN\"\n  children [\n    {body}\n  ]\n  boundingObject Group {{ children [ {bo} ] }}\n}}\n")


def plaza_node():
    x0, x1, y0, y1 = PLAZA
    cx, cy, sx, sy = (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0
    wy0, wy1 = y0, -9.3                       # walkway stripe starts where the entrance apron ends
    walk = (f"DEF EXTERIOR_WALKWAY Pose {{ translation 0 {(wy0 + wy1) / 2:g} {PLAZA_TOP + 0.001:g} children [ Shape {{ "
            f"appearance {appearance('walkway')} geometry Box {{ size {WALKWAY[1] - WALKWAY[0]:g} {wy1 - wy0:g} 0.002 }} }} ] }}")
    return (f"DEF EXTERIOR_PLAZA Solid {{\n  translation {cx:g} {cy:g} {PLAZA_TOP - 0.1:g}\n  name \"EXTERIOR_PLAZA\"\n"
            f"  children [ Shape {{ appearance {appearance('plaza', roughness=1)} geometry Box {{ size {sx:g} {sy:g} 0.2 }} }} ]\n"
            f"  boundingObject Box {{ size {sx:g} {sy:g} 0.2 }}\n}}\n{walk}\n")


def sign_texture():
    if SIGN.exists():
        return False
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1024, 256), (23, 43, 79))
    d = ImageDraw.Draw(img)
    fp = Path("C:/Windows/Fonts/arialbd.ttf")
    font = (lambda s: ImageFont.truetype(str(fp), s)) if fp.exists() else (lambda s: ImageFont.load_default(size=s))
    d.rectangle((0, 0, 1023, 255), outline=(240, 243, 246), width=10)
    d.rectangle((40, 196, 984, 204), fill=(36, 160, 196))
    text = "SECURITY CABIN"
    f = font(104)
    w = d.textlength(text, font=f)
    d.text(((1024 - w) / 2, 52), text, font=f, fill=(245, 247, 250))
    SIGN.parent.mkdir(parents=True, exist_ok=True)
    img.save(SIGN)
    return True


def rect_hits_polygon(rect, poly, margin):
    """Separating-axis test between an axis-aligned rect (x0,x1,y0,y1) grown by margin and a convex polygon."""
    x0, x1, y0, y1 = rect[0] - margin, rect[1] + margin, rect[2] - margin, rect[3] + margin
    rp = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    axes = [(1, 0), (0, 1)] + [(-(q[1] - p[1]), q[0] - p[0]) for p, q in zip(poly, poly[1:] + poly[:1])]
    for ax in axes:
        a = [p[0] * ax[0] + p[1] * ax[1] for p in rp]
        b = [p[0] * ax[0] + p[1] * ax[1] for p in poly]
        if max(a) < min(b) or max(b) < min(a):
            return False
    return True


def validate(cabin):
    poly = cabin.footprint()
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    report = {}
    # 1. fully on the plaza with room for SG-R1 to drive around it
    report["on_plaza_with_robot_margin"] = (min(xs) - ROBOT_RADIUS >= PLAZA[0] and max(xs) + ROBOT_RADIUS <= PLAZA[1]
                                            and min(ys) - ROBOT_RADIUS >= PLAZA[2])
    # 2. no overlap with any facility collider (walls, furniture, floors excluded) incl. robot radius
    hits = [c["name"] for c in facility.Facility().build().colliders if c["kind"] != "floor" and rect_hits_polygon(
        (c["center"][0] - c["size"][0] / 2, c["center"][0] + c["size"][0] / 2,
         c["center"][1] - c["size"][1] / 2, c["center"][1] + c["size"][1] / 2), poly, 2 * ROBOT_RADIUS)]
    report["clear_of_facility_colliders"] = not hits
    report["blocking_colliders"] = hits
    # 3. walkway from the entrance kept clear for the robot
    report["entrance_walkway_clear"] = not rect_hits_polygon((WALKWAY[0], WALKWAY[1], PLAZA[2], PLAZA[3]), poly, ROBOT_RADIUS)
    # 4. NPCs and robot spawn are far away
    pts = {name: pose[:2] for name, pose, *_ in pdw.NPCS}
    pts["SG-R1 spawn"] = ROBOT_SPAWN
    report["clear_of_npcs_and_robot_spawn"] = not any(rect_hits_polygon((x, x, y, y), poly, 1.0) for x, y in pts.values())
    # 5. space in front of the doorway (1.2 m) is free and on the plaza
    a = cabin.a
    door_c = cabin.W / 2 - WALL_T - 0.15 - a.door_width / 2
    c, s = math.cos(math.radians(a.yaw_deg)), math.sin(math.radians(a.yaw_deg))
    fx, fy = -cabin.L / 2 - 1.2, door_c
    front = (a.x + fx * c - fy * s, a.y + fx * s + fy * c)
    report["door_front_on_plaza"] = PLAZA[0] < front[0] < PLAZA[1] and PLAZA[2] < front[1] < PLAZA[3]
    report["door_width_m"] = a.door_width
    report["door_fits_npc"] = a.door_width >= 0.8 and DOOR_H >= 2.0   # NPC collision cylinder: 0.5 m wide, 1.72 m tall
    report["footprint_world"] = [[round(x, 3), round(y, 3)] for x, y in poly]
    report["gap_to_building_wall_m"] = round(-8.1 - max(ys), 3)
    report["gap_to_walkway_edge_m"] = round(min(xs) - WALKWAY[1], 3)
    report["collision_boxes"] = len(cabin.collide)
    report["visual_shapes"] = len(cabin.shapes)
    return report


def build_world(cabin):
    text = SOURCE.read_text(encoding="utf-8")
    text = re.sub(r'EXTERNPROTO "[^"\n]*SecureGuardSGR1\.proto"', 'EXTERNPROTO "../protos/SecureGuardSGR1.proto"', text, count=1)
    start = text.index("DEF SG_R1 SecureGuardSGR1 {")
    end = text.index("\n}\n", start) + 3
    text = text[:start] + pdw.ROBOT + text[end:]
    text = re.sub(r'title "[^"]*"', 'title "SecureGuard SG-R1 | Facility with Security Cabin"', text, count=1)
    return text.rstrip("\n") + "\n" + plaza_node() + cabin.node()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for k, val in DEFAULTS.items():
        ap.add_argument("--" + k.replace("_", "-"), type=float, default=val)
    ap.add_argument("--output", default=str(OUTPUT))
    a = ap.parse_args()
    cabin = Cabin(a).build()
    report = validate(cabin)
    checks = [k for k in ("on_plaza_with_robot_margin", "clear_of_facility_colliders", "entrance_walkway_clear",
                          "clear_of_npcs_and_robot_spawn", "door_front_on_plaza", "door_fits_npc") if not report[k]]
    print(json.dumps(report, indent=2))
    if checks:
        sys.exit("Placement rejected: " + ", ".join(checks) + ". Nothing written.")
    made_sign = sign_texture()
    world = build_world(cabin)
    assert world.count("{") == world.count("}") and world.count("[") == world.count("]"), "unbalanced world text"
    out = Path(a.output)
    if out.exists():
        backup = ROOT / "backups" / ("cabin-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        backup.mkdir(parents=True)
        shutil.copy2(out, backup / out.name)
        print("Backed up previous output to", backup)
    out.write_text(world, encoding="utf-8")
    print("Sign texture:", "created" if made_sign else "existing", SIGN.relative_to(ROOT))
    print("Wrote", out, "sha256", hashlib.sha256(world.encode()).hexdigest()[:16])


if __name__ == "__main__":
    main()
