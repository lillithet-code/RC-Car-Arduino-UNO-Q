#!/usr/bin/env python3
import json
import os
import socket
import time
from urllib import parse as urllib_parse
from urllib import request as urllib_request

try:
    import serial
except ImportError:  # pragma: no cover - exercised when pyserial is missing
    serial = None

try:
    import msgpack
except ImportError:  # pragma: no cover - exercised when msgpack is missing
    msgpack = None

try:
    from websocket import create_connection
except ImportError:  # pragma: no cover - exercised when websocket-client is missing
    create_connection = None

SERVER_BASE_URL = os.environ.get('SERVER_URL', 'https://drive.kbob.org').rstrip('/')
BOARD_TOKEN = os.environ.get('BOARD_TOKEN', '').strip()
SERIAL_PORT = os.environ.get('SERIAL_PORT', '/dev/ttyACM0')
SERIAL_BAUD_RATE = int(os.environ.get('SERIAL_BAUD_RATE', '9600'))
SERIAL_RETRY_SECONDS = float(os.environ.get('SERIAL_RETRY_SECONDS', '1.0'))
SERIAL_EXCLUSIVE = os.environ.get('SERIAL_EXCLUSIVE', '1').strip().lower() not in {'0', 'false', 'no', 'off'}
COMMAND_BRIDGE = os.environ.get('COMMAND_BRIDGE', '').strip()
ROUTER_SOCKET_PATH = os.environ.get('ROUTER_SOCKET_PATH', '/var/run/arduino-router.sock').strip()
BOARD_NAME = os.environ.get('BOARD_NAME', socket.gethostname() or 'rc-car-1')
BOARD_LOCATION = os.environ.get('BOARD_LOCATION', 'unknown')
COMMAND_WS_URL = os.environ.get('COMMAND_WS_URL', '').strip()
COMMAND_WS_RETRY_SECONDS = float(os.environ.get('COMMAND_WS_RETRY_SECONDS', '1.0'))

COMMAND_MAP = {
    'forward': 'F',
    'back': 'B',
    'left': 'L',
    'right': 'R',
    'stop': 'S',
    'lights_on': 'H',
    'lights_off': 'H',
}

RPC_METHOD_MAP = {
    'forward': ('car_forward', []),
    'back': ('car_back', []),
    'left': ('car_left', []),
    'right': ('car_right', []),
    'stop': ('car_stop', []),
    'lights_on': ('car_lights_on', []),
    'lights_off': ('car_lights_off', []),
}


def action_to_serial_command(action):
    if not action:
        return None
    return COMMAND_MAP.get(action.lower().strip())


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


def resolve_command_target():
    target = COMMAND_BRIDGE or SERIAL_PORT
    value = (target or '').strip()

    if not value or value.lower() in {'off', 'none', 'disabled'}:
        return {'transport': 'disabled', 'target': value}

    parsed = urllib_parse.urlparse(value)
    if value in {'router', 'unoq_rpc'}:
        return {'transport': 'rpc', 'path': ROUTER_SOCKET_PATH, 'target': value}
    if parsed.scheme == 'rpc':
        socket_path = parsed.path or parsed.netloc
        if not socket_path:
            raise ValueError(f'invalid rpc bridge target: {value}')
        return {'transport': 'rpc', 'path': socket_path, 'target': value}
    if parsed.scheme == 'tcp':
        if not parsed.hostname or not parsed.port:
            raise ValueError(f'invalid tcp bridge target: {value}')
        return {'transport': 'tcp', 'host': parsed.hostname, 'port': parsed.port, 'target': value}
    if parsed.scheme == 'udp':
        if not parsed.hostname or not parsed.port:
            raise ValueError(f'invalid udp bridge target: {value}')
        return {'transport': 'udp', 'host': parsed.hostname, 'port': parsed.port, 'target': value}
    if parsed.scheme == 'unix':
        socket_path = parsed.path or parsed.netloc
        if not socket_path:
            raise ValueError(f'invalid unix bridge target: {value}')
        return {'transport': 'unix', 'path': socket_path, 'target': value}

    return {'transport': 'serial', 'port': value, 'target': value}


TARGET_CONFIG = resolve_command_target()


def send_command(action, port=SERIAL_PORT, baud_rate=SERIAL_BAUD_RATE):
    command = action_to_serial_command(action)
    if not command:
        return False
    transport = TARGET_CONFIG.get('transport')
    payload = command.encode('ascii')

    if transport == 'disabled':
        return False

    if transport == 'tcp':
        with socket.create_connection((TARGET_CONFIG['host'], TARGET_CONFIG['port']), timeout=1.5) as connection:
            connection.sendall(payload)
        return True

    if transport == 'udp':
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.sendto(payload, (TARGET_CONFIG['host'], TARGET_CONFIG['port']))
        return True

    if transport == 'unix':
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(1.5)
            connection.connect(TARGET_CONFIG['path'])
            connection.sendall(payload)
        return True

    if transport == 'rpc':
        method = RPC_METHOD_MAP.get(action.lower().strip())
        if not method:
            return False
        if msgpack is None:
            raise RuntimeError('msgpack is required for rpc command transport')
        notify = [2, method[0], method[1]]
        payload = msgpack.packb(notify)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(1.5)
            connection.connect(TARGET_CONFIG['path'])
            connection.sendall(payload)
        return True

    if serial is None:
        raise RuntimeError('pyserial is required to send commands to the Arduino')

    with serial.Serial(port, baud_rate, timeout=1) as connection:
        connection.write(payload)
    return True


def open_command_connection(port=SERIAL_PORT, baud_rate=SERIAL_BAUD_RATE):
    transport = TARGET_CONFIG.get('transport')

    if transport == 'disabled':
        return None

    if transport == 'tcp':
        connection = socket.create_connection((TARGET_CONFIG['host'], TARGET_CONFIG['port']), timeout=2)
        connection.settimeout(1.0)
        return connection

    if transport == 'udp':
        connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        connection.settimeout(1.0)
        return connection

    if transport == 'unix':
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2.0)
        connection.connect(TARGET_CONFIG['path'])
        return connection

    if transport == 'rpc':
        if msgpack is None:
            raise RuntimeError('msgpack is required for rpc command transport')
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2.0)
        connection.connect(TARGET_CONFIG['path'])
        return connection

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

    transport = TARGET_CONFIG.get('transport')
    payload = command.encode('ascii')

    if transport == 'disabled':
        return False
    if transport == 'udp':
        connection.sendto(payload, (TARGET_CONFIG['host'], TARGET_CONFIG['port']))
        return True
    if transport in {'tcp', 'unix'}:
        connection.sendall(payload)
        return True

    if transport == 'rpc':
        method = RPC_METHOD_MAP.get(action.lower().strip())
        if not method:
            return False
        if msgpack is None:
            raise RuntimeError('msgpack is required for rpc command transport')
        notify = [2, method[0], method[1]]
        connection.sendall(msgpack.packb(notify))
        return True

    connection.write(payload)
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
        headers={
            **board_request_headers('RC-Car-Board/2.0'),
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

    connection = None
    command_ws = None

    register_device()

    transport = TARGET_CONFIG.get('transport')
    target = TARGET_CONFIG.get('target')
    if transport == 'disabled':
        print('command transport disabled; commands will be ignored')
    else:
        print(f'command transport={transport} target={target}')

    while True:
        if transport != 'disabled' and connection is None:
            try:
                connection = open_command_connection()
                if transport == 'serial':
                    print(f'serial connected: {SERIAL_PORT} @ {SERIAL_BAUD_RATE}')
                else:
                    print(f'bridge connected: {target}')
            except Exception as exc:  # pragma: no cover - runtime path
                print(f'command connect failed: {exc}')
                time.sleep(max(SERIAL_RETRY_SECONDS, 0.1))
                continue

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

            if transport == 'disabled':
                print(f'ignored action while transport disabled: {action}')
                continue

            if send_command_on_connection(action, connection):
                print(action)
        except Exception as exc:  # pragma: no cover - runtime path
            print(f'command websocket error: {exc}')
            if command_ws is not None:
                try:
                    command_ws.close()
                except Exception:
                    pass
            command_ws = None

            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
                connection = None

            time.sleep(max(COMMAND_WS_RETRY_SECONDS, 0.2))


if __name__ == '__main__':
    main()
