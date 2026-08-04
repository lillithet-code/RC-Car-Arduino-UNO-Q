#!/usr/bin/env python3
import json
import os
import socket
import time
from urllib import parse as urllib_parse
from urllib import request as urllib_request

try:
    from gpiozero import DigitalOutputDevice
except Exception:  # pragma: no cover - runtime environment dependent
    DigitalOutputDevice = None

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
GPIO_DRY_RUN = os.environ.get('GPIO_DRY_RUN', '0').strip().lower() in {'1', 'true', 'yes', 'on'}

DRIVE_IN1_PIN = int(os.environ.get('DRIVE_IN1_PIN', '17'))
DRIVE_IN2_PIN = int(os.environ.get('DRIVE_IN2_PIN', '27'))
STEER_IN1_PIN = int(os.environ.get('STEER_IN1_PIN', '22'))
STEER_IN2_PIN = int(os.environ.get('STEER_IN2_PIN', '23'))
LIGHTS_PIN = int(os.environ.get('LIGHTS_PIN', '24'))
GPIO_ACTIVE_HIGH = os.environ.get('GPIO_ACTIVE_HIGH', '1').strip().lower() not in {'0', 'false', 'no', 'off'}


class CarGPIODriver:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self._devices = {}

        if self.dry_run:
            print('GPIO dry-run enabled; commands will be logged only')
            return

        if DigitalOutputDevice is None:
            raise RuntimeError('gpiozero is required on Raspberry Pi (or set GPIO_DRY_RUN=1 for testing)')

        self._devices = {
            'drive_in1': DigitalOutputDevice(DRIVE_IN1_PIN, active_high=GPIO_ACTIVE_HIGH, initial_value=False),
            'drive_in2': DigitalOutputDevice(DRIVE_IN2_PIN, active_high=GPIO_ACTIVE_HIGH, initial_value=False),
            'steer_in1': DigitalOutputDevice(STEER_IN1_PIN, active_high=GPIO_ACTIVE_HIGH, initial_value=False),
            'steer_in2': DigitalOutputDevice(STEER_IN2_PIN, active_high=GPIO_ACTIVE_HIGH, initial_value=False),
            'lights': DigitalOutputDevice(LIGHTS_PIN, active_high=GPIO_ACTIVE_HIGH, initial_value=False),
        }

    def _set(self, name, value):
        device = self._devices.get(name)
        if device is None:
            return
        if value:
            device.on()
        else:
            device.off()

    def apply_action(self, action):
        if not action:
            return False

        command = action.strip().lower()
        if self.dry_run:
            print(f'gpio action={command}')
            return command in {'forward', 'back', 'left', 'right', 'stop', 'lights_on', 'lights_off'}

        if command == 'forward':
            self._set('drive_in1', True)
            self._set('drive_in2', False)
            return True
        if command == 'back':
            self._set('drive_in1', False)
            self._set('drive_in2', True)
            return True
        if command == 'left':
            self._set('steer_in1', True)
            self._set('steer_in2', False)
            return True
        if command == 'right':
            self._set('steer_in1', False)
            self._set('steer_in2', True)
            return True
        if command == 'stop':
            self._set('drive_in1', False)
            self._set('drive_in2', False)
            self._set('steer_in1', False)
            self._set('steer_in2', False)
            return True
        if command == 'lights_on':
            self._set('lights', True)
            return True
        if command == 'lights_off':
            self._set('lights', False)
            return True

        return False

    def close(self):
        if self.dry_run:
            return
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


def main():
    if not BOARD_TOKEN or BOARD_TOKEN == 'dev-board-token':
        raise SystemExit('BOARD_TOKEN must be configured with a non-default value')

    register_device()
    driver = CarGPIODriver(dry_run=GPIO_DRY_RUN)
    command_ws = None

    while True:
        if command_ws is None:
            try:
                command_ws = open_command_websocket()
                print('command websocket connected')
            except Exception as exc:  # pragma: no cover - runtime path
                print(f'command websocket connect failed: {exc}')
                time.sleep(max(COMMAND_WS_RETRY_SECONDS, 0.2))
                continue

        try:
            raw_message = command_ws.recv()
            if not raw_message:
                raise RuntimeError('empty websocket message')

            payload = json.loads(raw_message)
            action = payload.get('action')
            if not action:
                continue

            if driver.apply_action(action):
                print(action)
        except Exception as exc:  # pragma: no cover - runtime path
            print(f'command websocket error: {exc}')
            if command_ws is not None:
                try:
                    command_ws.close()
                except Exception:
                    pass
            command_ws = None
            time.sleep(max(COMMAND_WS_RETRY_SECONDS, 0.2))


if __name__ == '__main__':
    main()
