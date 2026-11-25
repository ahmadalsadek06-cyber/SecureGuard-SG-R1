
from controller import Robot

robot = Robot()
timestep = int(robot.getBasicTimeStep())

keyboard = robot.getKeyboard()
keyboard.enable(timestep)

left_motor = robot.getDevice("left wheel motor")
right_motor = robot.getDevice("right wheel motor")

left_motor.setPosition(float("inf"))
right_motor.setPosition(float("inf"))

MAX_SPEED = 6.28
SPEED = 3.0

left_motor.setVelocity(0.0)
right_motor.setVelocity(0.0)

print("SecureGuard WASD Ready", flush=True)

while robot.step(timestep) != -1:
    key = keyboard.getKey()

    left_speed = 0.0
    right_speed = 0.0

    if key in (ord("W"), ord("w")):
        left_speed = SPEED
        right_speed = SPEED

    elif key in (ord("S"), ord("s")):
        left_speed = -SPEED
        right_speed = -SPEED

    elif key in (ord("A"), ord("a")):
        left_speed = -SPEED
        right_speed = SPEED

    elif key in (ord("D"), ord("d")):
        left_speed = SPEED
        right_speed = -SPEED

    left_motor.setVelocity(left_speed)
    right_motor.setVelocity(right_speed)
