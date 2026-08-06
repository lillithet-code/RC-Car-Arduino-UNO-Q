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
from glob import glob
from dataclasses import dataclass
from urllib import parse as urllib_parse
from urllib import request as urllib_request

SERVER_BASE_URL = os.environ.get('SERVER_URL', 'https://drive.kbob.org').rstrip('/')
BOARD_TOKEN = os.environ.get('BOARD_TOKEN', '').strip()
BOARD_NAME = os.environ.get('BOARD_NAME', socket.gethostname() or 'rc-car-rpi')
BOARD_LOCATION = os.environ.get('BOARD_LOCATION', 'unknown')
STREAM_STATUS_POLL_SECONDS = max(0.3, float(os.environ.get('STREAM_STATUS_POLL_SECONDS', '1.0')))
STREAM_DISABLE_GRACE_SECONDS = max(0.0, float(os.environ.get('STREAM_DISABLE_GRACE_SECONDS', '8.0')))
STREAM_IDLE_POLL_SECONDS = max(STREAM_STATUS_POLL_SECONDS, float(os.environ.get('STREAM_IDLE_POLL_SECONDS', '2.0')))
FRAME_RATE = float(os.environ.get('FRAME_RATE', '30'))
VIDEO_WIDTH = int(os.environ.get('VIDEO_WIDTH', '1280'))
VIDEO_HEIGHT = int(os.environ.get('VIDEO_HEIGHT', '720'))
PICAMERA_BITRATE = int(os.environ.get('PICAMERA_BITRATE', '2000000'))
PICAMERA_PROFILE = (os.environ.get('PICAMERA_PROFILE', 'baseline') or '').strip()
PICAMERA_LEVEL = (os.environ.get('PICAMERA_LEVEL', '') or '').strip()
PICAMERA_SENSOR = (os.environ.get('PICAMERA_SENSOR', 'auto') or 'auto').strip().lower()
PICAMERA_CAMERA_INDEX = (os.environ.get('PICAMERA_CAMERA_INDEX', 'auto') or 'auto').strip().lower()
PUBLISH_RTSP_URL = os.environ.get('MEDIAMTX_RTSP_URL', '').strip()
LIBCAMERA_BIN = os.environ.get('LIBCAMERA_BIN', '').strip()
FFMPEG_BIN = os.environ.get('FFMPEG_BIN', 'ffmpeg').strip() or 'ffmpeg'

CAMERA_DISCOVERY_CACHE_SECONDS = max(2.0, float(os.environ.get('CAMERA_DISCOVERY_CACHE_SECONDS', '30.0')))
PUBLISH_READY_CONFIRM_SECONDS = max(1.0, float(os.environ.get('PUBLISH_READY_CONFIRM_SECONDS', '2.0')))
PUBLISH_BACKOFF_BASE_SECONDS = max(0.5, float(os.environ.get('PUBLISH_BACKOFF_BASE_SECONDS', '1.0')))
PUBLISH_BACKOFF_MAX_SECONDS = max(PUBLISH_BACKOFF_BASE_SECONDS, float(os.environ.get('PUBLISH_BACKOFF_MAX_SECONDS', '20.0')))
CPU_GOVERNOR_CONTROL_ENABLED = os.environ.get('CPU_GOVERNOR_CONTROL_ENABLED', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
CPU_GOVERNOR_ACTIVE = (os.environ.get('CPU_GOVERNOR_ACTIVE', 'ondemand') or '').strip()
CPU_GOVERNOR_IDLE = (os.environ.get('CPU_GOVERNOR_IDLE', 'powersave') or '').strip()

STREAM_CONFIG_ENDPOINT = f'{SERVER_BASE_URL}/api/board/stream/config'
STREAM_REPORT_ENDPOINT = f'{SERVER_BASE_URL}/api/board/stream/report'
DEVICE_REGISTER_ENDPOINT = f'{SERVER_BASE_URL}/api/devices/register'


def resolve_picamera_binary():
    if LIBCAMERA_BIN:
        return LIBCAMERA_BIN
    for candidate in ('rpicam-vid', 'libcamera-vid'):
        if shutil.which(candidate):
            return candidate
    return 'rpicam-vid'


PICAMERA_BIN = resolve_picamera_binary()


@dataclass(frozen=True)
class CameraMode:
    input_format: str
    width: int
    height: int
    fps: float


@dataclass(frozen=True)
class CameraDescriptor:
    index: int
    description: str
    modes: tuple


_CAMERA_DISCOVERY_CACHE = {
    'loaded_at': 0.0,
    'cameras': tuple(),
}


def parse_camera_index(raw_value):
    value = (raw_value or '').strip().lower()
    if value in {'', 'auto'}:
        return None
    try:
        index = int(value)
        return index if index >= 0 else None
    except ValueError:
        return None


def board_request_headers(user_agent):
    return {
        'Authorization': f'Bearer {BOARD_TOKEN}',
        'User-Agent': user_agent,
    }


def mode_to_dict(camera_mode):
    if camera_mode is None:
        return None
    return {
        'input_format': camera_mode.input_format,
        'width': camera_mode.width,
        'height': camera_mode.height,
        'fps': camera_mode.fps,
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


def _mode_distance(requested_mode, candidate_mode):
    width_delta = abs(candidate_mode.width - requested_mode.width)
    height_delta = abs(candidate_mode.height - requested_mode.height)
    fps_delta = abs(candidate_mode.fps - requested_mode.fps)
    return (width_delta * 10000) + (height_delta * 100) + fps_delta


def _parse_camera_modes(lines):
    mode_pattern = re.compile(r'([A-Za-z0-9_]+)\s+(\d+)x(\d+)\s*\[(\d+(?:\.\d+)?)\s*fps\]')
    modes = []
    for raw_line in lines:
        match = mode_pattern.search(raw_line.strip())
        if not match:
            continue
        width = int(match.group(2))
        height = int(match.group(3))
        fps = float(match.group(4))
        if width <= 0 or height <= 0 or fps <= 0:
            continue
        modes.append(CameraMode(
            input_format=match.group(1).strip().lower(),
            width=width,
            height=height,
            fps=fps,
        ))
    return tuple(modes)


def _query_list_cameras():
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
        return tuple()

    output = '\n'.join(part for part in ((result.stdout or ''), (result.stderr or '')) if part)
    lines = output.splitlines()

    cameras = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = re.search(r'^\s*(\d+)\s*:\s*(.+)$', line)
        if not match:
            idx += 1
            continue

        camera_index = int(match.group(1))
        description = match.group(2).strip()
        idx += 1

        mode_lines = []
        while idx < len(lines):
            nested = lines[idx]
            if re.search(r'^\s*\d+\s*:\s*.+$', nested):
                break
            mode_lines.append(nested)
            idx += 1

        cameras.append(CameraDescriptor(
            index=camera_index,
            description=description,
            modes=_parse_camera_modes(mode_lines),
        ))

    return tuple(cameras)


def discover_cameras(force_refresh=False):
    now = time.monotonic()
    if not force_refresh and (now - _CAMERA_DISCOVERY_CACHE['loaded_at']) <= CAMERA_DISCOVERY_CACHE_SECONDS:
        return _CAMERA_DISCOVERY_CACHE['cameras']

    cameras = _query_list_cameras()
    _CAMERA_DISCOVERY_CACHE['loaded_at'] = now
    _CAMERA_DISCOVERY_CACHE['cameras'] = cameras
    return cameras


def discover_sensor_hint(cameras):
    explicit_sensor = (PICAMERA_SENSOR or '').strip().lower()
    if explicit_sensor and explicit_sensor != 'auto':
        return explicit_sensor

    for camera in cameras:
        lowered = camera.description.lower()
        for sensor_name in ('imx219', 'imx708'):
            if sensor_name in lowered:
                return sensor_name
    return 'auto'


def resolve_selected_camera(cameras, sensor_hint):
    explicit_index = parse_camera_index(PICAMERA_CAMERA_INDEX)
    if explicit_index is not None:
        for camera in cameras:
            if camera.index == explicit_index:
                return camera
        return None

    normalized_hint = (sensor_hint or '').strip().lower()
    if normalized_hint not in {'', 'auto'}:
        for camera in cameras:
            if normalized_hint in camera.description.lower():
                return camera

    return cameras[0] if cameras else None


def probe_supported_h264_encoders():
    available = ['h264_passthrough']
    try:
        result = subprocess.run(
            [FFMPEG_BIN, '-hide_banner', '-encoders'],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        text = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        for encoder in ('h264_v4l2m2m', 'h264_omx', 'libx264'):
            if encoder in text:
                available.append(encoder)
    except Exception as exc:
        print(f'encoder probe failed: {exc}', file=sys.stderr)
    return sorted(set(available))


def resolve_requested_mode(config_payload):
    profile = (config_payload or {}).get('video_profile')
    if isinstance(profile, dict):
        override = parse_mode_value(profile.get('video_mode') or '')
        if override is not None:
            return override
    return CameraMode(input_format='h264', width=VIDEO_WIDTH, height=VIDEO_HEIGHT, fps=FRAME_RATE)


def select_best_mode(requested_mode, camera):
    if camera is None or not camera.modes:
        return requested_mode
    compatible = [mode for mode in camera.modes if mode.input_format in {'h264', 'yuv420', 'rgb'}]
    if not compatible:
        compatible = list(camera.modes)
    return min(compatible, key=lambda mode: _mode_distance(requested_mode, mode))


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
            **board_request_headers('RC-Car-RPi-Stream/2.0'),
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
    req = urllib_request.Request(url, headers=board_request_headers('RC-Car-RPi-Stream/2.0'))
    try:
        with urllib_request.urlopen(req, timeout=3) as response:
            payload = json.loads(response.read().decode('utf-8'))
        if payload.get('status') != 'ok':
            raise RuntimeError(f'unexpected stream config response: {payload}')
        return payload
    except Exception as exc:
        print(f'stream config check failed: {exc}', file=sys.stderr)
        return None


def report_stream_state(current_mode, available_modes, available_encoders):
    payload = {
        'board_name': BOARD_NAME,
        'current_h264_encoder': 'h264_passthrough',
        'current_video_mode': mode_to_dict(current_mode),
        'available_h264_encoders': available_encoders,
        'available_video_modes': [mode_to_dict(mode) for mode in available_modes if mode is not None],
        'video_device': 'picamera-csi',
    }
    req = urllib_request.Request(
        STREAM_REPORT_ENDPOINT,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            **board_request_headers('RC-Car-RPi-Stream/2.0'),
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


def build_libcamera_command(camera_mode, camera_index):
    command = [
        PICAMERA_BIN,
        '--nopreview',
        '--inline',
        '--codec', 'h264',
        '--intra', str(max(1, int(round(camera_mode.fps)))),
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
        '-use_wallclock_as_timestamps', '1',
        '-fflags', '+genpts+nobuffer',
        '-f', 'h264',
        '-i', '-',
        '-an',
        '-c:v', 'copy',
        '-rtsp_transport', 'tcp',
        '-f', 'rtsp',
        rtsp_url,
    ]


def _tail_logger(prefix, process, sink):
    if process is None or process.stderr is None:
        return

    def reader():
        try:
            for line in process.stderr:
                text = line.strip()
                if not text:
                    continue
                sink.append(text)
                if len(sink) > 50:
                    sink.pop(0)
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


def start_publisher(rtsp_url, camera_mode, camera_index):
    libcamera_cmd = build_libcamera_command(camera_mode, camera_index)
    ffmpeg_cmd = build_ffmpeg_publish_command(rtsp_url)

    print(
        f'starting CSI publish source={PICAMERA_BIN} '
        f'mode={camera_mode.width}x{camera_mode.height}@{camera_mode.fps:g} '
        f'bitrate={PICAMERA_BITRATE} rtsp={rtsp_url} camera_index={camera_index if camera_index is not None else "auto"}'
    )

    libcamera = subprocess.Popen(
        libcamera_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
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

    libcamera_stderr = []
    ffmpeg_stderr = []
    _tail_logger('libcamera:', libcamera, libcamera_stderr)
    _tail_logger('ffmpeg:', ffmpeg, ffmpeg_stderr)

    return {
        'libcamera': libcamera,
        'ffmpeg': ffmpeg,
        'libcamera_stderr': libcamera_stderr,
        'ffmpeg_stderr': ffmpeg_stderr,
        'started_at': time.monotonic(),
    }


def publisher_exited(publisher):
    if not publisher:
        return False
    libcamera = publisher.get('libcamera')
    ffmpeg = publisher.get('ffmpeg')
    return (libcamera is not None and libcamera.poll() is not None) or (ffmpeg is not None and ffmpeg.poll() is not None)


def publisher_snapshot(publisher):
    if not publisher:
        return {}
    libcamera = publisher.get('libcamera')
    ffmpeg = publisher.get('ffmpeg')
    return {
        'libcamera_returncode': None if libcamera is None else libcamera.poll(),
        'ffmpeg_returncode': None if ffmpeg is None else ffmpeg.poll(),
        'libcamera_stderr_tail': list(publisher.get('libcamera_stderr') or [])[-10:],
        'ffmpeg_stderr_tail': list(publisher.get('ffmpeg_stderr') or [])[-10:],
    }


def apply_cpu_governor(target, last_applied):
    desired = (target or '').strip()
    if not CPU_GOVERNOR_CONTROL_ENABLED or not desired:
        return last_applied
    if desired == last_applied:
        return last_applied

    governor_paths = sorted(glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor'))
    if not governor_paths:
        return last_applied

    applied = 0
    failed = 0
    for path in governor_paths:
        try:
            current_value = ''
            try:
                with open(path, 'r', encoding='utf-8') as handle:
                    current_value = (handle.read() or '').strip()
            except Exception:
                current_value = ''

            if current_value == desired:
                applied += 1
                continue

            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(desired)
            applied += 1
        except Exception as exc:
            failed += 1
            print(f'cpu governor apply failed path={path} target={desired} error={exc}', file=sys.stderr)

    if applied > 0 and failed == 0:
        print(f'cpu governor set target={desired} cpus={applied}')
        return desired
    return last_applied


def main():
    if not BOARD_TOKEN or BOARD_TOKEN == 'dev-board-token':
        raise SystemExit('BOARD_TOKEN must be configured with a non-default value')

    register_device()
    available_encoders = probe_supported_h264_encoders()

    publisher = None
    active_publish_url = None
    active_mode = None
    stream_enabled = False
    stream_disabled_since = None

    state = 'detecting'
    restart_attempt = 0
    bytes_last = 0
    bytes_last_change_at = None
    active_governor = None

    while True:
        config = fetch_stream_config()
        if config is None:
            state = 'retrying'
            if publisher is not None and publisher_exited(publisher):
                print(f'publisher exited while config unavailable: {publisher_snapshot(publisher)}', file=sys.stderr)
                stop_publisher(publisher, 'config-unavailable-exit')
                publisher = None
            time.sleep(STREAM_STATUS_POLL_SECONDS)
            continue

        stream_enabled = bool(config.get('enabled'))
        publish_url = PUBLISH_RTSP_URL or (config.get('rtsp_url') or '').strip()

        if stream_enabled:
            stream_disabled_since = None
        elif stream_disabled_since is None:
            stream_disabled_since = time.monotonic()

        if not stream_enabled:
            if publisher is not None and stream_disabled_since is not None:
                if (time.monotonic() - stream_disabled_since) >= STREAM_DISABLE_GRACE_SECONDS:
                    stop_publisher(publisher, 'disabled')
                    publisher = None
                    active_publish_url = None
                    active_mode = None
                    restart_attempt = 0
                    bytes_last = 0
                    bytes_last_change_at = None

            active_governor = apply_cpu_governor(CPU_GOVERNOR_IDLE, active_governor)
            state = 'idle' if publisher is None else 'draining'
            print(
                f'stream_state={state} enabled=0 '
                f'live={int(bool(config.get("stream_live")))} ready={int(bool(config.get("stream_ready")))} '
                f'bytes={int(config.get("mediamtx_bytes_received") or 0)}'
            )
            time.sleep(STREAM_IDLE_POLL_SECONDS)
            continue

        active_governor = apply_cpu_governor(CPU_GOVERNOR_ACTIVE, active_governor)

        if not publish_url:
            state = 'waiting-config'
            print('stream_state=waiting-config enabled=1 missing_rtsp_url=1', file=sys.stderr)
            time.sleep(STREAM_STATUS_POLL_SECONDS)
            continue

        cameras = discover_cameras()
        sensor_hint = discover_sensor_hint(cameras)
        selected_camera = resolve_selected_camera(cameras, sensor_hint)

        requested_mode = resolve_requested_mode(config)
        desired_mode = select_best_mode(requested_mode, selected_camera)
        mode_changed = active_mode != desired_mode
        url_changed = active_publish_url != publish_url

        available_modes = list(selected_camera.modes) if selected_camera and selected_camera.modes else [desired_mode]

        if stream_enabled and publish_url:
            if publisher is None or mode_changed or url_changed:
                state = 'starting'
                stop_publisher(publisher, 'reconfigure')
                publisher = start_publisher(
                    publish_url,
                    desired_mode,
                    selected_camera.index if selected_camera else None,
                )
                active_publish_url = publish_url
                active_mode = desired_mode
                restart_attempt = 0
                bytes_last = 0
                bytes_last_change_at = None

            bytes_received = int(config.get('mediamtx_bytes_received') or 0)
            if bytes_received > bytes_last:
                bytes_last = bytes_received
                bytes_last_change_at = time.monotonic()

            bytes_progressing = (
                bytes_last_change_at is not None
                and (time.monotonic() - bytes_last_change_at) <= PUBLISH_READY_CONFIRM_SECONDS
            )
            stream_live = bool(config.get('stream_live'))
            stream_ready = bool(config.get('stream_ready'))

            if stream_ready and stream_live and bytes_progressing:
                state = 'streaming'
            elif stream_ready or stream_live:
                state = 'degraded'
            else:
                state = 'starting'

            if publisher_exited(publisher):
                state = 'retrying'
                print(f'publisher exited unexpectedly: {publisher_snapshot(publisher)}', file=sys.stderr)
                stop_publisher(publisher, 'unexpected-exit')
                publisher = None
                restart_attempt += 1
                discover_cameras(force_refresh=True)
                backoff = min(PUBLISH_BACKOFF_MAX_SECONDS, PUBLISH_BACKOFF_BASE_SECONDS * (2 ** max(0, restart_attempt - 1)))
                time.sleep(backoff)
                continue

            if active_mode is not None:
                report_stream_state(active_mode, available_modes, available_encoders)
        print(
            f'stream_state={state} enabled={int(stream_enabled)} '
            f'live={int(bool(config.get("stream_live")))} ready={int(bool(config.get("stream_ready")))} '
            f'bytes={int(config.get("mediamtx_bytes_received") or 0)} mode={desired_mode.width}x{desired_mode.height}@{desired_mode.fps:g}'
        )
        time.sleep(STREAM_STATUS_POLL_SECONDS)


if __name__ == '__main__':
    main()
