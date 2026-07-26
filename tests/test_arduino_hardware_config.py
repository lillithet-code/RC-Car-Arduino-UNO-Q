from pathlib import Path


def test_arduino_config_exposes_camera_motor_servo_and_light_pins():
    root = Path(__file__).resolve().parents[1]
    header = (root / 'arduino' / 'car_config.h').read_text()
    sketch = (root / 'arduino' / 'uno_q_rc_car.ino').read_text()

    assert 'MOTOR_PWM_PIN' in header
    assert 'SERVO_STEERING_PIN' in header
    assert 'LIGHT_PIN_1' in header
    assert 'CAMERA_STREAM' in header or 'STREAM' in header
    assert '#include <Servo.h>' in sketch
