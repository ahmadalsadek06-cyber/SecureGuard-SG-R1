"""SG-R1 manual skid steering with smooth simultaneous WASD.

W/S throttle, A/D steer, any combination at once. Space brakes, C toggles camera.
Q/E/Z/X are single-key alternatives for WA/WD/SA/SD when keyboard rollover fails.

Steering convention (predictable in both directions):
  A always moves the robot toward ITS OWN LEFT side, D toward its right side.
  W+A curves forward-left (heading turns left / CCW).
  S+A reverses like a car with the wheel turned left: the robot backs toward its
  left side, so the heading turns right (CW). S+D mirrors this.
  A or D alone pivots in place (left = CCW, right = CW), slower than driving.

Control: keys -> target linear speed v (m/s) and yaw rate w (rad/s), each ramped
with separate accelerate/brake limits, then mixed to left/right wheel speeds
(both wheels on a side share one command) and scaled together to the motor limit
so the curve radius is preserved. Releasing all keys brakes v and w to zero.

No network, backend, autonomous navigation or SOC integration.
Distance sensors report meters; they do not override manual input.
"""
from controller import Robot

WHEEL_RADIUS = 0.13
TRACK = 0.43                 # wheel center-to-center, m
SKID_TRACK_FACTOR = 1.6      # skid-steer slip: effective track > geometric track
FORWARD_SPEED = 1.0          # m/s
REVERSE_SPEED = 0.6          # m/s; slower reverse for control
CURVE_YAW_RATE = 0.75        # rad/s while driving (radius ~1.3 m forward, ~0.8 m reverse)
PIVOT_YAW_RATE = 0.9         # rad/s turning in place
LINEAR_ACCEL = 0.8           # m/s^2
LINEAR_BRAKE = 1.5           # m/s^2 (release, opposite key, Space)
YAW_ACCEL = 2.0              # rad/s^2
YAW_BRAKE = 3.5              # rad/s^2
KEY_HOLD_GRACE = 0.08        # s: bridge single-sample key dropouts; release still stops promptly
MOTOR_NAMES = ('front_left_motor', 'rear_left_motor', 'front_right_motor', 'rear_right_motor')
DIAGONAL_KEYS = {'Q': 'WA', 'E': 'WD', 'Z': 'SA', 'X': 'SD'}
DRIVE_KEYS = set('WASDQEZX ')


def read_keys(keyboard):
    """Drain one Webots keyboard sample: the set of keys pressed right now.

    Webots reports held keys every sample (not OS key-repeat events), up to the
    hardware's rollover limit; modifier flags are stripped.
    """
    keys = set()
    key = keyboard.getKey()
    while key != -1:
        code = key & 0xffff
        if 0 <= code < 128:
            keys.add(chr(code).upper())
        key = keyboard.getKey()
    return keys


class HeldKeys:
    """Keys seen within KEY_HOLD_GRACE seconds count as held (filters dropped samples)."""
    def __init__(self, grace=KEY_HOLD_GRACE):
        self.grace = grace
        self.last_seen = {}

    def update(self, keys, now):
        for key in keys:
            self.last_seen[key] = now
        return {k for k, t in self.last_seen.items() if now - t <= self.grace + 1e-9}


def command(keys):
    """Return target (v m/s, w rad/s) for the held key set."""
    if ' ' in keys:
        return 0.0, 0.0
    active = set(keys)
    for alias, directions in DIAGONAL_KEYS.items():
        if alias in keys:
            active.update(directions)
    throttle = int('W' in active) - int('S' in active)
    steer = int('A' in active) - int('D' in active)
    if throttle == 0:
        return 0.0, steer * PIVOT_YAW_RATE
    v = FORWARD_SPEED if throttle > 0 else -REVERSE_SPEED
    # Car-like reverse: yaw sign follows travel direction so A always heads to the robot's left.
    return v, steer * CURVE_YAW_RATE * throttle


def _approach(current, target, accel, brake, dt):
    """Ramp toward target; brake limit when slowing or crossing zero, accel limit otherwise."""
    if current * target < 0:
        target_now = 0.0  # stop first, then accelerate the other way
    else:
        target_now = target
    limit = (brake if abs(target_now) < abs(current) else accel) * dt
    return current + max(-limit, min(limit, target_now - current))


def mix(v, w, max_wheel):
    """Skid-steer mixing to (left, right) wheel rad/s, scaled together to the motor limit."""
    half = TRACK * SKID_TRACK_FACTOR / 2
    left = (v - w * half) / WHEEL_RADIUS
    right = (v + w * half) / WHEEL_RADIUS
    peak = max(abs(left), abs(right))
    if peak > max_wheel:
        left, right = left * max_wheel / peak, right * max_wheel / peak
    return left, right


class Drive:
    """Smoothed velocity state. update() returns (left, right) wheel rad/s."""
    def __init__(self, max_wheel):
        self.max_wheel = max_wheel
        self.v = 0.0
        self.w = 0.0
        self.held = HeldKeys()

    def update(self, keys, now, dt):
        tv, tw = command(self.held.update(keys & DRIVE_KEYS, now))
        self.v = _approach(self.v, tv, LINEAR_ACCEL, LINEAR_BRAKE, dt)
        self.w = _approach(self.w, tw, YAW_ACCEL, YAW_BRAKE, dt)
        if tv == 0 and abs(self.v) < 1e-3:
            self.v = 0.0
        if tw == 0 and abs(self.w) < 1e-3:
            self.w = 0.0
        return mix(self.v, self.w, self.max_wheel)


def apply(motors, wheels):
    for index, motor in enumerate(motors):
        limit = motor.getMaxVelocity()
        motor.setVelocity(max(-limit, min(limit, wheels[0 if index < 2 else 1])))


def setup_motors(robot):
    motors = [robot.getDevice(name) for name in MOTOR_NAMES]
    for motor in motors:
        motor.setPosition(float('inf'))
        motor.setVelocity(0)
    return motors, min(motor.getMaxVelocity() for motor in motors)


def main():
    robot = Robot()
    timestep = int(robot.getBasicTimeStep())
    dt = timestep / 1000.0
    keyboard = robot.getKeyboard()
    keyboard.enable(timestep)
    motors, max_speed = setup_motors(robot)
    # Six single-ray sensors at ~10 Hz. Camera is off until C is pressed.
    sensor_period = max(1, round(96/timestep))*timestep
    for direction in ('front','rear','left','right','front_left','front_right'):
        robot.getDevice('distance_'+direction).enable(sensor_period)
    camera = robot.getDevice('front_camera')
    camera_enabled = False
    c_was_down = False
    drive = Drive(max_speed)
    print('SecureGuard SG-R1 WASD Ready | Combine W/S + A/D; Q/E/Z/X diagonals; Space brake; C camera', flush=True)
    while robot.step(timestep) != -1:
        keys = read_keys(keyboard)
        c_down = 'C' in keys
        if c_down and not c_was_down:
            camera_enabled = not camera_enabled
            if camera_enabled:
                camera.enable(max(1, round(192/timestep))*timestep)
            else:
                camera.disable()
            print('SG-R1 camera '+('on' if camera_enabled else 'off'), flush=True)
        c_was_down = c_down
        apply(motors, drive.update(keys, robot.getTime(), dt))


if __name__ == '__main__':
    main()
