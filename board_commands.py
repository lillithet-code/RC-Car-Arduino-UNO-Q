#!/usr/bin/env python3
import json
import os
import socket
import time
from urllib import request as urllib_request

try:
    import serial
except ImportError:  # pragma: no cover - exercised when pyserial is missing
    serial = None

SERVER_BASE_URL = os.environ.get('SERVER_URL', 'https://drive.kbob.org').rstrip('/')
BOARD_TOKEN = os.environ.get('BOARD_TOKEN', 'dev-board-token')
SERIAL_PORT = os.environ.get('SERIAL_PORT', '/dev/ttyACM0')
SERIAL_BAUD_RATE = int(os.environ.get('SERIAL_BAUD_RATE', '9600'))
SERIAL_RETRY_SECONDS = float(os.environ.get('SERIAL_RETRY_SECONDS', '1.0'))
SERIAL_EXCLUSIVE = os.environ.get('SERIAL_EXCLUSIVE', '1').strip().lower() not in {'0', 'false', 'no', 'off'}
BOARD_NAME = os.environ.get('BOARD_NAME', socket.gethostname() or 'rc-car-1')
BOARD_LOCATION = os.environ.get('BOARD_LOCATION', 'unknown')

COMMAND_MAP = {
    'forward': 'F',
    'back': 'B',
    'left': 'L',
    'right': 'R',
    'stop': 'S',
    'lights_on': 'H',
    'lights_off': 'H',
}


def action_to_serial_command(action):
    if not action:
        return None
    return COMMAND_MAP.get(action.lower().strip())


def fetch_command():
    url = f'{SERVER_BASE_URL}/api/board/command?token={BOARD_TOKEN}'
    req = urllib_request.Request(url, headers={'User-Agent': 'RC-Car-Board/1.0'})
    try:
        with urllib_request.urlopen(req, timeout=1) as response:
            payload = json.loads(response.read().decode('utf-8'))
            return payload.get('action')
    except Exception:
        return None


def send_command(action, port=SERIAL_PORT, baud_rate=SERIAL_BAUD_RATE):
    command = action_to_serial_command(action)
    if not command:
        return False
    if serial is None:
        raise RuntimeError('pyserial is required to send commands to the Arduino')

    with serial.Serial(port, baud_rate, timeout=1) as connection:
        connection.write(command.encode('ascii'))
    return True


def open_serial_connection(port=SERIAL_PORT, baud_rate=SERIAL_BAUD_RATE):
    if serial is None:
        raise RuntimeError('pyserial is required to send commands to the Arduino')

    try:
        return serial.Serial(
            port,
            baud_rate,
            timeout=1,
            write_timeout=1,
            exclusive=SERIAL_EXCLUSIVE,
            dsrdtr=False,
            rtscts=False,
        )
    except TypeError:
        return serial.Serial(
            port,
            baud_rate,
            timeout=1,
            write_timeout=1,
            dsrdtr=False,
            rtscts=False,
        )


def send_command_on_connection(action, connection):
    command = action_to_serial_command(action)
    if not command:
        return False
    connection.write(command.encode('ascii'))
    return True


def register_device():
    url = f'{SERVER_BASE_URL}/api/devices/register'
    payload = json.dumps({
        'name': BOARD_NAME,
        'kind': 'arduino',
        'location': BOARD_LOCATION,
    }).encode('utf-8')
    req = urllib_request.Request(
        url,
        data=payload,
        headers={'Content-Type': 'application/json', 'User-Agent': 'RC-Car-Board/1.0'},
        method='POST',
    )
    try:
        with urllib_request.urlopen(req, timeout=3):
            return True
    except Exception as exc:
        print(f'device register failed: {exc}')
        return False


def main():
    connection = None

    register_device()

    while True:
        if connection is None:
            try:
                connection = open_serial_connection()
                print(f'serial connected: {SERIAL_PORT} @ {SERIAL_BAUD_RATE}')
            except Exception as exc:  # pragma: no cover - runtime path
                print(f'serial connect failed: {exc}')
                time.sleep(max(SERIAL_RETRY_SECONDS, 0.1))
                continue

        action = fetch_command()
        if action:
            try:
                if send_command_on_connection(action, connection):
                    print(action)
            except Exception as exc:  # pragma: no cover - runtime path
                print(f'command failed: {exc}')
                try:
                    connection.close()
                except Exception:
                    pass
                connection = None
        time.sleep(0.05)


if __name__ == '__main__':
    main()
