"""SG-R1 WASD patrol + simulated person detection and zone authorization.

Driving reuses sgr1_controller unchanged (simultaneous W/S + A/D, Q/E/Z/X, Space, C).
Detection uses Webots Camera Recognition on 'front_camera': only Solids with
recognitionColors inside camera FOV/range (occlusion-checked by Webots) are reported.
Identities are simulated tags (Solid 'model'), NOT facial recognition.
Supervisor is used only to read the recognized node's world position for zoning.
"""
import json
import math
import sys
from pathlib import Path

import controller

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'sgr1_controller'))
from sgr1_controller import Drive, apply, read_keys, setup_motors  # noqa: E402
import person_auth as auth  # noqa: E402
from direct_elastic import select_event_sink  # noqa: E402  (backend bridge by default)

CAMERA_PERIOD_MS = 192
STATUS_PERIOD_S = 5.0  # robot_status heartbeat (simulation time)
SEVERITY = {auth.AUTHORIZED: 'info', auth.UNKNOWN: 'warning', auth.UNAUTHORIZED: 'high'}


def person_event(event_type, config, tag, zone, dist, status):
    identity = config['identities'].get(tag, {})
    person = {'person_id': tag, 'classification': status, 'zone': zone or 'UNMAPPED', 'distance_m': round(dist, 2)}
    if identity:
        person.update(role=identity['role'], badge_id=identity['badge_id'])
    return person


def pose(robot):
    node = robot.getSelf()
    p, o, v = node.getPosition(), node.getOrientation(), node.getVelocity()
    yaw = math.atan2(o[3], o[0])
    return p, yaw, v[0]*math.cos(yaw) + v[1]*math.sin(yaw)


def clamp_xy(p):
    return {'x': round(max(-15, min(15, p[0])), 3), 'y': round(max(-12, min(12, p[1])), 3)}


def detections(robot, camera, config):
    """Yield (tag, distance_m, world_xy, zone, status) for each recognized person."""
    for obj in camera.getRecognitionObjects():
        tag = obj.getModel()
        dist = auth.distance(list(obj.getPosition()))
        node = robot.getFromId(obj.getId())
        position = node.getPosition() if node is not None else None
        zone = auth.zone_at(config, position[0], position[1]) if position else None
        yield tag, dist, position, zone, auth.classify(config, tag, zone)


def main():
    robot = controller.Supervisor()
    timestep = int(robot.getBasicTimeStep())
    dt = timestep / 1000.0
    config = auth.load_config()
    gate = auth.AlertGate(config['alert_cooldown_s'])
    keyboard = robot.getKeyboard()
    keyboard.enable(timestep)
    motors, max_speed = setup_motors(robot)
    drive = Drive(max_speed)
    sensor_period = max(1, round(96/timestep))*timestep
    for direction in ('front', 'rear', 'left', 'right', 'front_left', 'front_right'):
        robot.getDevice('distance_'+direction).enable(sensor_period)
    camera = robot.getDevice('front_camera')
    camera_period = max(1, round(CAMERA_PERIOD_MS/timestep))*timestep
    has_recognition = camera.hasRecognition()

    def set_camera(on):
        if on:
            camera.enable(camera_period)
            if has_recognition:
                camera.recognitionEnable(camera_period)
        else:
            if has_recognition:
                camera.recognitionDisable()
            camera.disable()

    camera_enabled = True  # detection needs the camera; C still toggles it
    set_camera(True)
    if not has_recognition:
        print('SG-R1 WARNING: front_camera has no Recognition node; person detection disabled', flush=True)
    c_was_down = False
    last_status = {}
    # SG_EVENT_DESTINATION=backend (default: FastAPI bridge) or elastic (direct to Elasticsearch)
    bridge = select_event_sink(lambda m: print(m, flush=True))
    next_status, robot_zone = 0.0, None
    print('SecureGuard SG-R1 Person Detection Ready | WASD drive; C camera+detection; '
          'identities are simulated tags, not facial recognition', flush=True)
    while robot.step(timestep) != -1:
        keys = read_keys(keyboard)
        c_down = 'C' in keys
        if c_down and not c_was_down:
            camera_enabled = not camera_enabled
            set_camera(camera_enabled)
            print('SG-R1 camera+detection '+('on' if camera_enabled else 'off'), flush=True)
        c_was_down = c_down
        apply(motors, drive.update(keys, robot.getTime(), dt))

        if bridge is not None:  # cheap: builds a dict and appends to a queue; network is on another thread
            p, yaw, speed = pose(robot)
            zone_now = auth.zone_at(config, p[0], p[1]) or 'UNMAPPED'
            common = {'zone': zone_now, 'position': clamp_xy(p)}
            if zone_now != robot_zone:
                bridge.submit({'event_type': 'patrol_status', 'severity': 'info', **common,
                               'message': f'SG-R1 entered {zone_now}'})
                robot_zone = zone_now
            if robot.getTime() >= next_status:
                bridge.submit({'event_type': 'robot_status', 'severity': 'info', **common,
                               'robot': {'speed_mps': round(speed, 3), 'heading_rad': round(yaw, 4),
                                         'camera_enabled': camera_enabled, 'sim_time_s': round(robot.getTime(), 3)}})
                next_status = robot.getTime() + STATUS_PERIOD_S

        if not (camera_enabled and has_recognition):
            continue
        now = robot.getTime()
        seen = {}
        for tag, dist, position, zone, status in detections(robot, camera, config):
            seen[tag] = (zone, status)
            if last_status.get(tag) != (zone, status):
                zone_name = config['zones'][zone]['name'] if zone else 'unmapped'
                print(f'DETECT {tag} | {status} | zone {zone or "?"} {zone_name} | {dist:.2f} m', flush=True)
                if bridge is not None and position:
                    bridge.submit({'event_type': 'person_detected', 'severity': SEVERITY[status],
                                   'zone': zone or 'UNMAPPED', 'position': clamp_xy(position),
                                   'person': person_event('person_detected', config, tag, zone, dist, status)})
            if status == auth.UNAUTHORIZED and gate.allow(tag, now):
                print('ALERT unauthorized_access '+json.dumps(auth.alert_record(config, tag, zone, dist, now, position)), flush=True)
                if bridge is not None:
                    bridge.submit({'event_type': 'unauthorized_access', 'severity': 'high', 'zone': zone,
                                   'position': clamp_xy(position),
                                   'person': person_event('unauthorized_access', config, tag, zone, dist, status),
                                   'message': f'{tag} unauthorized in {config["zones"][zone]["name"]} (simulated)'})
        for tag in set(last_status) - set(seen):
            print(f'LOST {tag} | no longer visible', flush=True)
        last_status = seen
    if bridge is not None:
        bridge.close()  # flush briefly on simulation exit


if __name__ == '__main__':
    main()
