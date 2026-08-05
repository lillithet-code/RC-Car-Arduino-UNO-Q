#!/usr/bin/env python3
import importlib.util
import json
import os
import socket
import time
from urllib import parse as urllib_parse
from urllib import request as urllib_request


def normalize_gpiozero_pin_factory(env=None):
    source = os.environ if env is None else env
    raw_value = (source.get('GPIOZERO_PIN_FACTORY') or '').strip().lower()
    if raw_value != 'lgpio':
        return raw_value

    if importlib.util.find_spec('lgpio') is not None:
        return raw_value

    fallback = 'native'
    source['GPIOZERO_PIN_FACTORY'] = fallback
    print('warning: GPIOZERO_PIN_FACTORY=lgpio but lgpio is unavailable; falling back to native pin factory')
    return fallback


normalize_gpiozero_pin_factory()

try:
    from gpiozero import AngularServo, DigitalOutputDevice, PWMOutputDevice
except Exception:  # pragma: no cover - runtime environment dependent
    AngularServo = None
    DigitalOutputDevice = None
    PWMOutputDevice = None

try:
    from gpiozero.exc import PinPWMUnsupported
except Exception:  # pragma: no cover - runtime environment dependent
    PinPWMUnsupported = Exception

try:
    from websocket import create_connection
except ImportError:  # pragma: no cover - exercised when websocket-client is missing
    create_connection = None

SERVER_BASE_URL = os.environ.get('SERVER_URL', 'https://drive.kbob.org').rstrip('/')
BOARD_TOKEN = os.environ.get('BOARD_TOKEN', '').strip()
BOARD_NAME = os.environ.get('BOARD_NAME', socket.gethostname() or 'rc-car-rpi')
BOARD_LOCATION = os.environ.get('BOARD_LOCATION', 'unknown')
COMMAND_WS_URL = os.environ.get('COMMAND_WS_URL', '').strip()
COMMAND_WS_RETRY_SECONDS = float(os.environ.get('COMMAND_WS_RETRY_SECONDS', '1.0'))
MOTOR_WATCHDOG_MS = max(300, int(os.environ.get('MOTOR_WATCHDOG_MS', '500')))
GPIO_DRY_RUN = os.environ.get('GPIO_DRY_RUN', '0').strip().lower() in {'1', 'true', 'yes', 'on'}

DRIVE_IN1_PIN = int(os.environ.get('DRIVE_IN1_PIN', '17'))
DRIVE_IN2_PIN = int(os.environ.get('DRIVE_IN2_PIN', '27'))
SERVO_PIN = int(os.environ.get('SERVO_PIN', '12'))
SERVO_LEFT_ANGLE = float(os.environ.get('SERVO_LEFT_ANGLE', '-30'))
SERVO_CENTER_ANGLE = float(os.environ.get('SERVO_CENTER_ANGLE', '0'))
SERVO_RIGHT_ANGLE = float(os.environ.get('SERVO_RIGHT_ANGLE', '30'))
SERVO_MIN_ANGLE = float(os.environ.get('SERVO_MIN_ANGLE', '-90'))
SERVO_MAX_ANGLE = float(os.environ.get('SERVO_MAX_ANGLE', '90'))
SERVO_MIN_PULSE_WIDTH = float(os.environ.get('SERVO_MIN_PULSE_WIDTH', '0.0005'))
SERVO_MAX_PULSE_WIDTH = float(os.environ.get('SERVO_MAX_PULSE_WIDTH', '0.0025'))
SERVO_FRAME_WIDTH = float(os.environ.get('SERVO_FRAME_WIDTH', '0.02'))
FORWARD_THROTTLE = max(0.0, min(1.0, float(os.environ.get('FORWARD_THROTTLE', '0.65'))))
BACK_THROTTLE = max(0.0, min(1.0, float(os.environ.get('BACK_THROTTLE', '0.5'))))
LIGHTS_PIN = int(os.environ.get('LIGHTS_PIN', '24'))
GPIO_ACTIVE_HIGH = os.environ.get('GPIO_ACTIVE_HIGH', '1').strip().lower() not in {'0', 'false', 'no', 'off'}


class CarGPIODriver:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self._devices = {}
        self._pwm_capable = {}
        self._servo = None
        self._warned_servo_unavailable = False

        if self.dry_run:
            print('GPIO dry-run enabled; commands will be logged only')
            return

        if DigitalOutputDevice is None or PWMOutputDevice is None or AngularServo is None:
            raise RuntimeError('gpiozero is required on Raspberry Pi (or set GPIO_DRY_RUN=1 for testing)')

        self._devices = {
            'drive_in1': self._build_drive_output('drive_in1', DRIVE_IN1_PIN),
            'drive_in2': self._build_drive_output('drive_in2', DRIVE_IN2_PIN),
            'lights': DigitalOutputDevice(LIGHTS_PIN, active_high=GPIO_ACTIVE_HIGH, initial_value=False),
        }
        try:
            self._servo = AngularServo(
                SERVO_PIN,
                min_angle=SERVO_MIN_ANGLE,
                max_angle=SERVO_MAX_ANGLE,
                min_pulse_width=SERVO_MIN_PULSE_WIDTH,
                max_pulse_width=SERVO_MAX_PULSE_WIDTH,
                frame_width=SERVO_FRAME_WIDTH,
            )
            self._servo.angle = SERVO_CENTER_ANGLE
        except PinPWMUnsupported:
            self._servo = None
            print(
                f'warning: PWM not supported on GPIO{SERVO_PIN} for servo steering; '
                'continuing without servo output. Install a PWM-capable pin factory (lgpio/pigpio) or adjust SERVO_PIN.'
            )

    def _build_drive_output(self, name, pin):
        try:
            device = PWMOutputDevice(pin, active_high=GPIO_ACTIVE_HIGH, initial_value=0.0, frequency=1000)
            self._pwm_capable[name] = True
            return device
        except PinPWMUnsupported:
            print(
                f'warning: PWM not supported on GPIO{pin}; falling back to digital on/off for {name}. '
                'Use GPIO12/13/18/19 for true hardware PWM throttle.'
            )
            self._pwm_capable[name] = False
            return DigitalOutputDevice(pin, active_high=GPIO_ACTIVE_HIGH, initial_value=False)

    def _set(self, name, value):
        device = self._devices.get(name)
        if device is None:
            return
        if self._pwm_capable.get(name):
            device.value = max(0.0, min(1.0, float(value)))
            return
        if float(value) >= 0.5:
            device.on()
            return
        device.off()

    def _set_throttle(self, forward_duty, reverse_duty):
        self._set('drive_in1', forward_duty)
        self._set('drive_in2', reverse_duty)

    def _set_steering(self, angle):
        if self._servo is None:
            if not self._warned_servo_unavailable:
                print('warning: steering command ignored because servo output is unavailable')
                self._warned_servo_unavailable = True
            return
        clamped = max(SERVO_MIN_ANGLE, min(SERVO_MAX_ANGLE, float(angle)))
        self._servo.angle = clamped

    def apply_action(self, action):
        if not action:
            return False

        command = action.strip().lower()
        if self.dry_run:
            print(f'gpio action={command}')
            return command in {'forward', 'back', 'left', 'right', 'stop', 'lights_on', 'lights_off'}

        if command == 'forward':
            self._set_throttle(FORWARD_THROTTLE, 0.0)
            return True
        if command == 'back':
            self._set_throttle(0.0, BACK_THROTTLE)
            return True
        if command == 'left':
            self._set_steering(SERVO_LEFT_ANGLE)
            return True
        if command == 'right':
            self._set_steering(SERVO_RIGHT_ANGLE)
            return True
        if command == 'stop':
            self._set_throttle(0.0, 0.0)
            self._set_steering(SERVO_CENTER_ANGLE)
            return True
        if command == 'lights_on':
            self._set('lights', True)
            return True
        if command == 'lights_off':
            self._set('lights', False)
            return True

        return False

    def apply_state(self, throttle=0.0, steering=0.0, lights=False, stop=False):
        if self.dry_run:
            print(
                f'gpio state throttle={float(throttle):.3f} steering={float(steering):.3f} '
                f'lights={bool(lights)} stop={bool(stop)}'
            )
            return True

        try:
            throttle = float(throttle)
            steering = float(steering)
        except (TypeError, ValueError):
            return False

        throttle = max(-1.0, min(1.0, throttle))
        steering = max(-1.0, min(1.0, steering))

        if stop:
            throttle = 0.0

        if throttle > 0:
            self._set_throttle(FORWARD_THROTTLE * throttle, 0.0)
        elif throttle < 0:
            self._set_throttle(0.0, BACK_THROTTLE * abs(throttle))
        else:
            self._set_throttle(0.0, 0.0)

        servo_angle = SERVO_CENTER_ANGLE + ((SERVO_RIGHT_ANGLE - SERVO_LEFT_ANGLE) * 0.5 * steering)
        self._set_steering(servo_angle)
        self._set('lights', bool(lights))
        return True

    def emergency_stop(self, keep_lights=False):
        if self.dry_run:
            print(f'gpio emergency_stop keep_lights={bool(keep_lights)}')
            return
        self._set_throttle(0.0, 0.0)
        self._set_steering(SERVO_CENTER_ANGLE)
        if not keep_lights:
            self._set('lights', False)

    def close(self):
        if self.dry_run:
            return
        if self._servo is not None:
            try:
                self._servo.close()
            except Exception:
                pass
        for device in self._devices.values():
            try:
                device.close()
            except Exception:
                pass


def build_command_ws_url():
    if COMMAND_WS_URL:
        base = COMMAND_WS_URL.rstrip('/')
    else:
        parsed = urllib_parse.urlparse(SERVER_BASE_URL)
        scheme = 'wss' if parsed.scheme == 'https' else 'ws'
        netloc = parsed.netloc or parsed.path
        base = f'{scheme}://{netloc}/ws/board/commands'

    separator = '&' if '?' in base else '?'
    board_param = urllib_parse.quote(BOARD_NAME, safe='')
    return f'{base}{separator}board_name={board_param}'


def board_request_headers(user_agent):
    return {
        'Authorization': f'Bearer {BOARD_TOKEN}',
        'User-Agent': user_agent,
    }


def register_device():
    url = f'{SERVER_BASE_URL}/api/devices/register'
    payload = json.dumps({
        'name': BOARD_NAME,
        'kind': 'raspberry_pi_4b_picam3',
        'location': BOARD_LOCATION,
    }).encode('utf-8')
    req = urllib_request.Request(
        url,
        data=payload,
        headers={
            **board_request_headers('RC-Car-RPi-Board/1.0'),
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    try:
        with urllib_request.urlopen(req, timeout=3):
            return True
    except Exception as exc:
        print(f'device register failed: {exc}')
        return False


def open_command_websocket():
    if create_connection is None:
        raise RuntimeError('websocket-client is required for command websocket transport')
    ws_url = build_command_ws_url()
    return create_connection(
        ws_url,
        timeout=5,
        header=[f'Authorization: Bearer {BOARD_TOKEN}'],
    )


def parse_desired_state(payload, last_sequence, now_ms):
    if not isinstance(payload, dict):
        return None, 'invalid_payload'

    try:
        sequence = int(payload.get('sequence'))
    except (TypeError, ValueError):
        return None, 'invalid_sequence'

    if sequence < last_sequence:
        return None, 'out_of_order'

    try:
        expires_in_ms = int(payload.get('expires_in_ms', MOTOR_WATCHDOG_MS))
    except (TypeError, ValueError):
        return None, 'invalid_expires'
    expires_in_ms = max(200, min(2000, expires_in_ms))

    try:
        issued_at_ms = int(payload.get('issued_at_ms', now_ms))
    except (TypeError, ValueError):
        issued_at_ms = now_ms

    expires_at_ms = issued_at_ms + expires_in_ms
    if expires_at_ms < now_ms:
        return None, 'expired'

    try:
        throttle = float(payload.get('throttle', 0.0))
        steering = float(payload.get('steering', 0.0))
    except (TypeError, ValueError):
        return None, 'invalid_axes'

    throttle = max(-1.0, min(1.0, throttle))
    steering = max(-1.0, min(1.0, steering))
    stop = bool(payload.get('stop', False) or abs(throttle) < 1e-6)

    state = {
        'sequence': sequence,
        'expires_at_ms': expires_at_ms,
        'throttle': 0.0 if stop else throttle,
        'steering': steering,
        'lights': bool(payload.get('lights', False)),
        'stop': stop,
    }
    return state, None


def main():
    if not BOARD_TOKEN or BOARD_TOKEN == 'dev-board-token':
        raise SystemExit('BOARD_TOKEN must be configured with a non-default value')

    register_device()
    driver = CarGPIODriver(dry_run=GPIO_DRY_RUN)
    command_ws = None
    last_sequence = 0
    watchdog_deadline = time.monotonic() + (MOTOR_WATCHDOG_MS / 1000.0)

    try:
        while True:
            if command_ws is None:
                try:
                    command_ws = open_command_websocket()
                    command_ws.settimeout(0.2)
                    print('command websocket connected')
                except Exception as exc:  # pragma: no cover - runtime path
                    print(f'command websocket connect failed: {exc}')
                    driver.emergency_stop()
                    time.sleep(max(COMMAND_WS_RETRY_SECONDS, 0.2))
                    continue

            if time.monotonic() > watchdog_deadline:
                driver.emergency_stop(keep_lights=True)

            try:
                raw_message = command_ws.recv()
                if not raw_message:
                    raise RuntimeError('empty websocket message')

                now_ms = int(time.time() * 1000)
                payload = json.loads(raw_message)

                desired_state, error_reason = parse_desired_state(payload, last_sequence, now_ms)
                if desired_state is None:
                    if error_reason in {'expired', 'invalid_payload', 'invalid_axes', 'invalid_expires'}:
                        driver.emergency_stop(keep_lights=True)
                    continue

                if driver.apply_state(
                    throttle=desired_state['throttle'],
                    steering=desired_state['steering'],
                    lights=desired_state['lights'],
                    stop=desired_state['stop'],
                ):
                    last_sequence = desired_state['sequence']
                    watchdog_deadline = time.monotonic() + (MOTOR_WATCHDOG_MS / 1000.0)
                    print(
                        f"cmd seq={desired_state['sequence']} throttle={desired_state['throttle']:.3f} "
                        f"steering={desired_state['steering']:.3f} lights={int(desired_state['lights'])} "
                        f"stop={int(desired_state['stop'])}"
                    )
            except Exception as exc:  # pragma: no cover - runtime path
                message = str(exc).lower()
                if 'timed out' in message or 'timeout' in message:
                    continue

                print(f'command websocket error: {exc}')
                driver.emergency_stop(keep_lights=True)
                if command_ws is not None:
                    try:
                        command_ws.close()
                    except Exception:
                        pass
                command_ws = None
                time.sleep(max(COMMAND_WS_RETRY_SECONDS, 0.2))
    finally:
        try:
            driver.emergency_stop()
        except Exception:
            pass
        driver.close()


if __name__ == '__main__':
    main()
