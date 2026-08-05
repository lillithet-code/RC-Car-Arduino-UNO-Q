#!/usr/bin/env python3
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from urllib import parse as urllib_parse
from urllib import request as urllib_request

SERVER_BASE_URL = os.environ.get('SERVER_URL', 'https://drive.kbob.org').rstrip('/')
BOARD_TOKEN = os.environ.get('BOARD_TOKEN', '').strip()
BOARD_NAME = os.environ.get('BOARD_NAME', socket.gethostname() or 'rc-car-rpi')
BOARD_LOCATION = os.environ.get('BOARD_LOCATION', 'unknown')
STREAM_STATUS_POLL_SECONDS = max(0.3, float(os.environ.get('STREAM_STATUS_POLL_SECONDS', '1.0')))
STREAM_DISABLE_GRACE_SECONDS = max(0.0, float(os.environ.get('STREAM_DISABLE_GRACE_SECONDS', '8.0')))
FRAME_RATE = float(os.environ.get('FRAME_RATE', '60'))
VIDEO_WIDTH = int(os.environ.get('VIDEO_WIDTH', '1280'))
VIDEO_HEIGHT = int(os.environ.get('VIDEO_HEIGHT', '720'))
IMX219_FRAME_RATE = float(os.environ.get('PICAMERA_IMX219_FRAME_RATE', '30'))
IMX219_VIDEO_WIDTH = int(os.environ.get('PICAMERA_IMX219_VIDEO_WIDTH', '1280'))
IMX219_VIDEO_HEIGHT = int(os.environ.get('PICAMERA_IMX219_VIDEO_HEIGHT', '720'))
PICAMERA_BITRATE = int(os.environ.get('PICAMERA_BITRATE', '2000000'))
PICAMERA_PROFILE = (os.environ.get('PICAMERA_PROFILE', 'baseline') or '').strip()
PICAMERA_LEVEL = (os.environ.get('PICAMERA_LEVEL', '') or '').strip()
PICAMERA_SENSOR = (os.environ.get('PICAMERA_SENSOR', 'auto') or 'auto').strip().lower()
PICAMERA_CAMERA_INDEX = (os.environ.get('PICAMERA_CAMERA_INDEX', 'auto') or 'auto').strip().lower()
PUBLISH_RTSP_URL = os.environ.get('MEDIAMTX_RTSP_URL', '').strip()
LIBCAMERA_BIN = os.environ.get('LIBCAMERA_BIN', '').strip()
FFMPEG_BIN = os.environ.get('FFMPEG_BIN', 'ffmpeg').strip() or 'ffmpeg'

STREAM_CONFIG_ENDPOINT = f'{SERVER_BASE_URL}/api/board/stream/config'
STREAM_REPORT_ENDPOINT = f'{SERVER_BASE_URL}/api/board/stream/report'
DEVICE_REGISTER_ENDPOINT = f'{SERVER_BASE_URL}/api/devices/register'


def resolve_picamera_binary():
    if LIBCAMERA_BIN:
        return LIBCAMERA_BIN

    for candidate in ('rpicam-vid', 'libcamera-vid'):
        if shutil.which(candidate):
            return candidate

    # Keep an explicit default for clear runtime errors when neither binary exists.
    return 'rpicam-vid'


PICAMERA_BIN = resolve_picamera_binary()


def parse_camera_index(raw_value):
    value = (raw_value or '').strip().lower()
    if value in {'', 'auto'}:
        return None
    try:
        index = int(value)
        return index if index >= 0 else None
    except ValueError:
        return None


def _list_camera_descriptions():
    try:
        result = subprocess.run(
            [PICAMERA_BIN, '--list-cameras'],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        print(f'camera list query failed: {exc}', file=sys.stderr)
        return []

    output = '\n'.join(part for part in ((result.stdout or ''), (result.stderr or '')) if part)
    cameras = []
    for line in output.splitlines():
        match = re.search(r'^\s*(\d+)\s*:\s*(.+)$', line)
        if not match:
            continue
        cameras.append({
            'index': int(match.group(1)),
            'description': match.group(2).strip().lower(),
        })
    return cameras


def discover_picamera_sensor():
    explicit_sensor = (PICAMERA_SENSOR or '').strip().lower()
    if explicit_sensor and explicit_sensor != 'auto':
        return explicit_sensor

    for camera in _list_camera_descriptions():
        description = camera['description']
        for sensor_name in ('imx219', 'imx708'):
            if sensor_name in description:
                return sensor_name

    return 'auto'


def discover_camera_index_for_sensor(sensor_hint):
    sensor = (sensor_hint or '').strip().lower()
    if sensor in {'', 'auto'}:
        return None

    for camera in _list_camera_descriptions():
        if sensor in camera['description']:
            return camera['index']

    print(f'camera sensor hint not found in list-cameras output: {sensor_hint}', file=sys.stderr)
    return None


def resolve_camera_index(sensor_hint=None):
    explicit_index = parse_camera_index(PICAMERA_CAMERA_INDEX)
    if explicit_index is not None:
        return explicit_index
    resolved_sensor = sensor_hint or discover_picamera_sensor()
    return discover_camera_index_for_sensor(resolved_sensor)


@dataclass(frozen=True)
class CameraMode:
    input_format: str
    width: int
    height: int
    fps: float


def board_request_headers(user_agent):
    return {
        'Authorization': f'Bearer {BOARD_TOKEN}',
        'User-Agent': user_agent,
    }


def parse_mode_value(mode_value):
    raw = (mode_value or '').strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(',')]
    if len(parts) != 4:
        return None
    try:
        return CameraMode(
            input_format=parts[0].lower(),
            width=int(parts[1]),
            height=int(parts[2]),
            fps=float(parts[3]),
        )
    except (TypeError, ValueError):
        return None


def format_mode_value(camera_mode):
    if camera_mode is None:
        return None
    return f'{camera_mode.input_format},{camera_mode.width},{camera_mode.height},{camera_mode.fps:g}'


def mode_to_dict(camera_mode):
    if camera_mode is None:
        return None
    return {
        'input_format': camera_mode.input_format,
        'width': camera_mode.width,
        'height': camera_mode.height,
        'fps': camera_mode.fps,
    }


def resolve_requested_camera_mode(env=None, sensor_hint=None):
    source = os.environ if env is None else env
    resolved_sensor = (sensor_hint or source.get('PICAMERA_SENSOR', PICAMERA_SENSOR) or '').strip().lower()
    if resolved_sensor == 'auto':
        resolved_sensor = discover_picamera_sensor()

    if resolved_sensor == 'imx219':
        width = int(source.get('PICAMERA_IMX219_VIDEO_WIDTH', str(IMX219_VIDEO_WIDTH)))
        height = int(source.get('PICAMERA_IMX219_VIDEO_HEIGHT', str(IMX219_VIDEO_HEIGHT)))
        fps = float(source.get('PICAMERA_IMX219_FRAME_RATE', str(IMX219_FRAME_RATE)))
    else:
        width = int(source.get('VIDEO_WIDTH', str(VIDEO_WIDTH)))
        height = int(source.get('VIDEO_HEIGHT', str(VIDEO_HEIGHT)))
        fps = float(source.get('FRAME_RATE', str(FRAME_RATE)))
    return CameraMode(input_format='h264', width=width, height=height, fps=fps)


def register_device():
    payload = json.dumps({
        'name': BOARD_NAME,
        'kind': 'raspberry_pi_4b_picam3',
        'location': BOARD_LOCATION,
    }).encode('utf-8')
    req = urllib_request.Request(
        DEVICE_REGISTER_ENDPOINT,
        data=payload,
        headers={
            **board_request_headers('RC-Car-RPi-Stream/1.0'),
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    try:
        with urllib_request.urlopen(req, timeout=3):
            return True
    except Exception as exc:
        print(f'device register failed: {exc}', file=sys.stderr)
        return False


def fetch_stream_config():
    board_name = urllib_parse.quote(BOARD_NAME, safe='')
    url = f'{STREAM_CONFIG_ENDPOINT}?board_name={board_name}'
    req = urllib_request.Request(url, headers=board_request_headers('RC-Car-RPi-Stream/1.0'))
    try:
        with urllib_request.urlopen(req, timeout=3) as response:
            payload = json.loads(response.read().decode('utf-8'))
        if payload.get('status') != 'ok':
            raise RuntimeError(f'unexpected stream config response: {payload}')
        return payload
    except Exception as exc:
        print(f'stream config check failed: {exc}', file=sys.stderr)
        return None


def report_stream_state(current_mode):
    payload = {
        'board_name': BOARD_NAME,
        'current_h264_encoder': 'h264_passthrough',
        'current_video_mode': mode_to_dict(current_mode),
        'available_h264_encoders': ['h264_passthrough'],
        'available_video_modes': [mode_to_dict(current_mode)] if current_mode else [],
        'video_device': 'picamera3',
    }
    req = urllib_request.Request(
        STREAM_REPORT_ENDPOINT,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            **board_request_headers('RC-Car-RPi-Stream/1.0'),
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    try:
        with urllib_request.urlopen(req, timeout=3):
            return True
    except Exception as exc:
        print(f'stream state report failed: {exc}', file=sys.stderr)
        return False


def resolve_profile_from_config(config_payload, sensor_hint=None):
    requested_mode = resolve_requested_camera_mode(sensor_hint=sensor_hint)

    resolved_sensor = (sensor_hint or '').strip().lower()
    if resolved_sensor in {'', 'auto'}:
        resolved_sensor = discover_picamera_sensor()
    if resolved_sensor == 'imx219':
        return requested_mode

    profile = (config_payload or {}).get('video_profile')
    if isinstance(profile, dict):
        mode_override = parse_mode_value(profile.get('video_mode') or '')
        if mode_override is not None:
            requested_mode = mode_override

    return requested_mode


def build_libcamera_command(camera_mode):
    camera_index = resolve_camera_index()
    command = [
        PICAMERA_BIN,
        '--nopreview',
        '--inline',
        '--codec', 'h264',
        '--width', str(camera_mode.width),
        '--height', str(camera_mode.height),
        '--framerate', f'{camera_mode.fps:g}',
        '--bitrate', str(PICAMERA_BITRATE),
        '--timeout', '0',
        '--output', '-',
    ]

    if camera_index is not None:
        command.extend(['--camera', str(camera_index)])

    if PICAMERA_PROFILE:
        command.extend(['--profile', PICAMERA_PROFILE])

    if PICAMERA_LEVEL:
        command.extend(['--level', PICAMERA_LEVEL])

    return command


def build_ffmpeg_publish_command(rtsp_url):
    return [
        FFMPEG_BIN,
        '-hide_banner',
        '-loglevel', 'error',
        '-fflags', 'nobuffer',
        '-f', 'h264',
        '-i', '-',
        '-an',
        '-c:v', 'copy',
        '-rtsp_transport', 'tcp',
        '-f', 'rtsp',
        rtsp_url,
    ]


def stream_stderr(prefix, process):
    if process is None or process.stderr is None:
        return

    def reader():
        try:
            for line in process.stderr:
                text = line.strip()
                if text:
                    print(f'{prefix} {text}', file=sys.stderr)
        except Exception as exc:
            print(f'{prefix} stderr reader failed: {exc}', file=sys.stderr)

    threading.Thread(target=reader, daemon=True).start()


def stop_publisher(publisher, reason):
    if not publisher:
        return

    for process in (publisher.get('ffmpeg'), publisher.get('libcamera')):
        if process is None:
            continue
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            print(f'publisher did not stop quickly ({reason}); killing', file=sys.stderr)
            process.kill()
            try:
                process.wait(timeout=1.0)
            except Exception:
                pass
        except Exception:
            pass


def start_publisher(rtsp_url, camera_mode):
    libcamera_cmd = build_libcamera_command(camera_mode)
    ffmpeg_cmd = build_ffmpeg_publish_command(rtsp_url)
    selected_camera_index = None
    if '--camera' in libcamera_cmd:
        selected_camera_index = libcamera_cmd[libcamera_cmd.index('--camera') + 1]

    print(
        f'starting Pi Camera 3 publish source={PICAMERA_BIN} '
        f'mode={camera_mode.width}x{camera_mode.height}@{camera_mode.fps:g} '
        f'bitrate={PICAMERA_BITRATE} rtsp={rtsp_url} '
        f'camera_selector={PICAMERA_CAMERA_INDEX} sensor_hint={PICAMERA_SENSOR} '
        f'camera_index={selected_camera_index if selected_camera_index is not None else "auto"}'
    )

    libcamera = subprocess.Popen(
        libcamera_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    ffmpeg = subprocess.Popen(
        ffmpeg_cmd,
        stdin=libcamera.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    if libcamera.stdout is not None:
        libcamera.stdout.close()

    stream_stderr('libcamera:', libcamera)
    stream_stderr('ffmpeg:', ffmpeg)

    return {
        'libcamera': libcamera,
        'ffmpeg': ffmpeg,
    }


def publisher_exited(publisher):
    if not publisher:
        return False
    libcamera = publisher.get('libcamera')
    ffmpeg = publisher.get('ffmpeg')
    return (libcamera is not None and libcamera.poll() is not None) or (ffmpeg is not None and ffmpeg.poll() is not None)


def main():
    if not BOARD_TOKEN or BOARD_TOKEN == 'dev-board-token':
        raise SystemExit('BOARD_TOKEN must be configured with a non-default value')

    register_device()

    publisher = None
    active_publish_url = None
    active_mode_value = None
    stream_enabled = False
    stream_disabled_since = None

    while True:
        config = fetch_stream_config()
        sensor_hint = discover_picamera_sensor()
        if config is not None:
            stream_enabled = bool(config.get('enabled'))
            configured_rtsp_url = (config.get('rtsp_url') or '').strip()
            publish_url = PUBLISH_RTSP_URL or configured_rtsp_url
            desired_mode = resolve_profile_from_config(config, sensor_hint=sensor_hint)
        else:
            publish_url = active_publish_url
            desired_mode = resolve_profile_from_config(None, sensor_hint=sensor_hint)

        if stream_enabled:
            stream_disabled_since = None
        elif stream_disabled_since is None:
            stream_disabled_since = time.monotonic()

        desired_mode_value = format_mode_value(desired_mode)
        publish_changed = active_publish_url != publish_url
        mode_changed = active_mode_value != desired_mode_value

        if stream_enabled and publish_url:
            if publisher is None or publish_changed or mode_changed:
                stop_publisher(publisher, 'reconfigure')
                publisher = start_publisher(publish_url, desired_mode)
                active_publish_url = publish_url
                active_mode_value = desired_mode_value

            report_stream_state(desired_mode)

            if publisher_exited(publisher):
                print('Pi camera publisher exited unexpectedly; restarting', file=sys.stderr)
                stop_publisher(publisher, 'unexpected-exit')
                publisher = None
                time.sleep(0.5)
        else:
            if publisher is not None and stream_disabled_since is not None:
                disabled_for = time.monotonic() - stream_disabled_since
                if disabled_for >= STREAM_DISABLE_GRACE_SECONDS:
                    stop_publisher(publisher, 'disabled')
                    publisher = None
                    active_publish_url = None
                    print('Pi camera publisher stopped while stream disabled')

        time.sleep(STREAM_STATUS_POLL_SECONDS)


if __name__ == '__main__':
    main()
