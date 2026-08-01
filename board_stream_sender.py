#!/usr/bin/env python3
import glob
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import cv2


SERVER_BASE_URL = os.environ.get('SERVER_URL', 'https://drive.kbob.org').rstrip('/')
BOARD_TOKEN = os.environ.get('BOARD_TOKEN', '').strip()
BOARD_NAME = os.environ.get('BOARD_NAME', socket.gethostname() or 'rc-car-1')
BOARD_LOCATION = os.environ.get('BOARD_LOCATION', 'unknown')
VIDEO_DEVICE_SETTING = os.environ.get('VIDEO_DEVICE', 'auto').strip().lower()
VIDEO_DEVICE_AUTO_RECOVER = os.environ.get('VIDEO_DEVICE_AUTO_RECOVER', '1').strip().lower() not in {'0', 'false', 'no', 'off'}
VIDEO_MODE_AUTO = os.environ.get('VIDEO_MODE_AUTO', '1').strip().lower() not in {'0', 'false', 'no', 'off'}
FRAME_RATE = float(os.environ.get('FRAME_RATE', '30'))
VIDEO_WIDTH = int(os.environ.get('VIDEO_WIDTH', '1920'))
VIDEO_HEIGHT = int(os.environ.get('VIDEO_HEIGHT', '1080'))
STREAM_STATUS_POLL_SECONDS = max(0.3, float(os.environ.get('STREAM_STATUS_POLL_SECONDS', '1.0')))
STREAM_DISABLE_GRACE_SECONDS = max(0.0, float(os.environ.get('STREAM_DISABLE_GRACE_SECONDS', '8.0')))
VIDEO_REPROBE_DELAY_SECONDS = max(0.2, float(os.environ.get('VIDEO_REPROBE_DELAY_SECONDS', '1.0')))
FFMPEG_BIN = os.environ.get('FFMPEG_BIN', 'ffmpeg').strip()
H264_ENCODER = os.environ.get('H264_ENCODER', 'libx264').strip()
H264_BITRATE = os.environ.get('H264_BITRATE', '2500k').strip()
H264_MAXRATE = os.environ.get('H264_MAXRATE', H264_BITRATE).strip()
H264_BUFSIZE = os.environ.get('H264_BUFSIZE', '1500k').strip()
H264_GOP = int(os.environ.get('H264_GOP', str(max(1, int(FRAME_RATE)))))
H264_PROFILE = os.environ.get('H264_PROFILE', 'baseline').strip()


@dataclass(frozen=True)
class CameraMode:
    input_format: str
    width: int
    height: int
    fps: float


def resolve_input_format():
    direct = (os.environ.get('H264_INPUT_FORMAT') or '').strip().lower()
    if direct:
        return direct

    legacy = (os.environ.get('H264_INPUT_FORMATS') or '').strip().lower()
    if legacy:
        first = legacy.split(',')[0].strip()
        if first:
            return first

    return 'mjpeg'


H264_INPUT_FORMAT = resolve_input_format()
PUBLISH_RTSP_URL = os.environ.get('MEDIAMTX_RTSP_URL', '').strip()

STREAM_CONFIG_ENDPOINT = f'{SERVER_BASE_URL}/api/board/stream/config'
DEVICE_REGISTER_ENDPOINT = f'{SERVER_BASE_URL}/api/devices/register'


def board_request_headers(user_agent):
    return {
        'Authorization': f'Bearer {BOARD_TOKEN}',
        'User-Agent': user_agent,
    }


def normalize_input_format(value):
    normalized = (value or '').strip().lower()
    aliases = {
        'mjpg': 'mjpeg',
        'jpeg': 'mjpeg',
        'mjpeg': 'mjpeg',
        'yuyv': 'yuyv422',
        'yuy2': 'yuyv422',
        'yuyv422': 'yuyv422',
        'nv12': 'nv12',
        'h264': 'h264',
    }
    return aliases.get(normalized, normalized)


def parse_v4l2_modes(output):
    modes = []
    current_format = None
    current_size = None

    for raw_line in (output or '').splitlines():
        line = raw_line.strip()
        format_match = re.search(r"\[\d+\]:\s*'([^']+)'", line)
        if format_match:
            current_format = normalize_input_format(format_match.group(1))
            current_size = None
            continue

        size_match = re.search(r'Size:\s+Discrete\s+(\d+)x(\d+)', line, re.IGNORECASE)
        if size_match:
            current_size = (int(size_match.group(1)), int(size_match.group(2)))
            continue

        stepwise_match = re.search(r'Size:\s+Stepwise\s+(\d+)x(\d+)\s*-\s*(\d+)x(\d+)', line, re.IGNORECASE)
        if stepwise_match:
            min_width, min_height, max_width, max_height = [int(stepwise_match.group(i)) for i in (1, 2, 3, 4)]
            current_size = (min_width, min_height)
            if max_width >= min_width and max_height >= min_height:
                modes.append(CameraMode(
                    input_format=current_format,
                    width=max_width,
                    height=max_height,
                    fps=30.0,
                ))
            current_size = None
            continue

        fps_match = re.search(r'\(([0-9]+(?:\.[0-9]+)?)\s+fps\)', line, re.IGNORECASE)
        if fps_match and current_format and current_size:
            modes.append(CameraMode(
                input_format=current_format,
                width=current_size[0],
                height=current_size[1],
                fps=float(fps_match.group(1)),
            ))

    unique_modes = []
    seen = set()
    for mode in modes:
        key = (mode.input_format, mode.width, mode.height, round(mode.fps, 3))
        if key not in seen:
            seen.add(key)
            unique_modes.append(mode)
    return unique_modes


def resolve_requested_camera_mode(env=None):
    source = os.environ if env is None else env

    requested_width = int(source.get('VIDEO_WIDTH', str(VIDEO_WIDTH)))
    requested_height = int(source.get('VIDEO_HEIGHT', str(VIDEO_HEIGHT)))
    requested_fps = float(source.get('FRAME_RATE', str(FRAME_RATE)))
    requested_format = normalize_input_format(source.get('H264_INPUT_FORMAT') or H264_INPUT_FORMAT)

    legacy_low_resolution = (
        (requested_width, requested_height) in {(800, 600), (640, 480)}
        or requested_width < 1280
        or requested_height < 720
    )

    if legacy_low_resolution:
        requested_width = 1920
        requested_height = 1080
        requested_fps = max(requested_fps, 30.0)

    return CameraMode(
        input_format=requested_format,
        width=requested_width,
        height=requested_height,
        fps=requested_fps,
    )


def read_v4l2_modes(video_device_index):
    device = f'/dev/video{video_device_index}'
    try:
        result = subprocess.run(
            ['v4l2-ctl', '-d', device, '--list-formats-ext'],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except FileNotFoundError:
        print('v4l2-ctl not found; using configured camera mode without validation', file=sys.stderr)
        return None
    except Exception as exc:
        print(f'camera capability probe failed: {exc}', file=sys.stderr)
        return None

    if result.returncode != 0:
        stderr_tail = (result.stderr or '').strip()
        raise RuntimeError(f'v4l2-ctl failed for {device}: {stderr_tail or "unknown error"}')

    modes = parse_v4l2_modes(result.stdout)
    if not modes:
        raise RuntimeError(f'no discrete capture modes were reported for {device}')
    return modes


def choose_camera_mode(modes, requested_mode):
    requested_format = normalize_input_format(requested_mode.input_format)
    format_order = [requested_format, 'mjpeg', 'yuyv422', 'nv12', 'h264']
    candidates = []
    for input_format in format_order:
        candidates = [mode for mode in modes if mode.input_format == input_format]
        if candidates:
            break

    if not candidates:
        return None

    exact = [
        mode for mode in candidates
        if mode.width == requested_mode.width
        and mode.height == requested_mode.height
        and abs(mode.fps - requested_mode.fps) < 0.05
    ]
    if exact:
        return exact[0]

    for mode in candidates:
        if mode.width >= requested_mode.width and mode.height >= requested_mode.height:
            return CameraMode(
                input_format=requested_mode.input_format,
                width=requested_mode.width,
                height=requested_mode.height,
                fps=min(requested_mode.fps, mode.fps),
            )

    # Driving benefits more from a smooth frame rate than a larger, slow frame.
    minimum_smooth_fps = min(max(requested_mode.fps, 1.0), 25.0)
    smooth = [mode for mode in candidates if mode.fps >= minimum_smooth_fps]
    if smooth:
        within_requested_size = [
            mode for mode in smooth
            if mode.width <= requested_mode.width and mode.height <= requested_mode.height
        ]
        pool = within_requested_size or smooth
        return max(
            pool,
            key=lambda mode: (
                mode.width * mode.height,
                mode.width / max(1, mode.height),
                mode.fps,
            ),
        )

    # If the camera cannot reach a smooth rate, select its fastest usable mode.
    within_requested_size = [
        mode for mode in candidates
        if mode.width <= requested_mode.width and mode.height <= requested_mode.height
    ]
    pool = within_requested_size or candidates
    return max(
        pool,
        key=lambda mode: (
            mode.fps,
            mode.width * mode.height,
            mode.width / max(1, mode.height),
        ),
    )


def resolve_camera_mode(video_device_index):
    requested = resolve_requested_camera_mode()
    modes = read_v4l2_modes(video_device_index)
    if modes is None:
        return requested

    selected = choose_camera_mode(modes, requested)
    if selected is None:
        available_formats = ', '.join(sorted({mode.input_format for mode in modes}))
        raise RuntimeError(
            f'camera /dev/video{video_device_index} does not provide a supported capture format; '
            f'configured={requested.input_format}, available={available_formats or "none"}'
        )

    if selected != requested:
        message = (
            f'configured camera mode {requested.input_format} {requested.width}x{requested.height}@{requested.fps:g} '
            f'is unsupported; selected {selected.input_format} {selected.width}x{selected.height}@{selected.fps:g}'
        )
        if not VIDEO_MODE_AUTO:
            raise RuntimeError(message + '; set VIDEO_MODE_AUTO=1 or configure an exact supported mode')
        print(message, file=sys.stderr)
    else:
        print(
            f'camera mode validated: {selected.input_format} '
            f'{selected.width}x{selected.height}@{selected.fps:g}'
        )
    return selected


def parse_video_device_setting(value):
    normalized = (value or '').strip().lower()
    if not normalized or normalized == 'auto':
        return None, True

    path_match = re.match(r'^/dev/video(\d+)$', normalized)
    if path_match:
        return int(path_match.group(1)), False

    try:
        return int(normalized), False
    except ValueError:
        return None, True


def list_video_device_indices():
    paths = sorted(glob.glob('/dev/video*'))
    indices = []
    for path in paths:
        match = re.match(r'^/dev/video(\d+)$', path)
        if match:
            index = int(match.group(1))
            if is_capture_video_device(index):
                indices.append(index)
    return indices


def is_capture_video_device(device_index):
    device = f'/dev/video{device_index}'
    try:
        result = subprocess.run(
            ['v4l2-ctl', '-d', device, '--all'],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except FileNotFoundError:
        # If v4l2-ctl is not available, keep backward-compatible behavior.
        return True
    except Exception:
        return False

    if result.returncode != 0:
        return False

    details = (result.stdout or '').lower()
    return 'video capture' in details


def ffmpeg_probe_video_device(device_index):
    device = f'/dev/video{device_index}'
    try:
        result = subprocess.run(
            [
                FFMPEG_BIN,
                '-hide_banner',
                '-loglevel', 'error',
                '-f', 'v4l2',
                '-i', device,
                '-frames:v', '1',
                '-f', 'null',
                '-',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        )
    except Exception:
        return False

    return result.returncode == 0


def probe_video_device(device_index):
    backend = cv2.CAP_V4L2 if hasattr(cv2, 'CAP_V4L2') else 0

    # Some boards expose capture devices that OpenCV cannot open by numeric index
    # but can open by explicit path.
    for source in (f'/dev/video{device_index}', device_index):
        cap = cv2.VideoCapture(source, backend)
        if not cap.isOpened():
            cap.release()
            continue

        ok = False
        for _ in range(12):
            ret, _ = cap.read()
            if ret:
                ok = True
                break
            time.sleep(0.05)

        cap.release()
        if ok:
            return True

    # Fallback probe for environments where OpenCV capture backends are flaky.
    return ffmpeg_probe_video_device(device_index)


def resolve_working_video_device(preferred_index=None):
    if preferred_index is not None and probe_video_device(preferred_index):
        return preferred_index

    for index in list_video_device_indices():
        if preferred_index is not None and index == preferred_index:
            continue
        if probe_video_device(index):
            return index
    return None


def register_device():
    payload = json.dumps({
        'name': BOARD_NAME,
        'kind': 'arduino',
        'location': BOARD_LOCATION,
    }).encode('utf-8')
    req = urllib_request.Request(
        DEVICE_REGISTER_ENDPOINT,
        data=payload,
        headers={
            **board_request_headers('RC-Car-Board-Stream/2.0'),
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
    req = urllib_request.Request(url, headers=board_request_headers('RC-Car-Board-Stream/2.0'))
    try:
        with urllib_request.urlopen(req, timeout=3) as response:
            payload = json.loads(response.read().decode('utf-8'))
        if payload.get('status') != 'ok':
            raise RuntimeError(f'unexpected stream config response: {payload}')
        return payload
    except Exception as exc:
        print(f'stream config check failed: {exc}', file=sys.stderr)
        return None


def stop_publisher(process, reason):
    if process is None:
        return

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
    except Exception as exc:
        print(f'publisher stop error ({reason}): {exc}', file=sys.stderr)


def build_publish_command(video_device_index, rtsp_url, camera_mode=None):
    if camera_mode is None:
        camera_mode = resolve_requested_camera_mode()
    command = [
        FFMPEG_BIN,
        '-hide_banner',
        '-loglevel', 'error',
        '-fflags', 'nobuffer',
        '-f', 'v4l2',
        '-input_format', camera_mode.input_format,
        '-video_size', f'{camera_mode.width}x{camera_mode.height}',
        '-framerate', f'{camera_mode.fps:g}',
        '-i', f'/dev/video{video_device_index}',
        '-an',
        '-c:v', H264_ENCODER,
        '-g', str(max(1, H264_GOP)),
        '-bf', '0',
        '-b:v', H264_BITRATE,
        '-maxrate', H264_MAXRATE,
        '-bufsize', H264_BUFSIZE,
    ]

    if H264_ENCODER == 'libx264':
        command.extend([
            '-pix_fmt', 'yuv420p',
            '-profile:v', H264_PROFILE,
            '-preset', 'veryfast',
            '-tune', 'zerolatency',
        ])

    command.extend([
        '-rtsp_transport', 'tcp',
        '-f', 'rtsp',
        rtsp_url,
    ])
    return command


def start_publisher(video_device_index, rtsp_url, camera_mode):
    command = build_publish_command(video_device_index, rtsp_url, camera_mode)
    print(f'starting MediaMTX publish: {" ".join(command)}')
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)


def stream_ffmpeg_stderr(process, video_device_index, rtsp_url):
    if process is None or process.stderr is None:
        return

    def reader():
        try:
            for line in process.stderr:
                text = line.strip()
                if not text:
                    continue
                print(
                    f'ffmpeg[{video_device_index} -> {rtsp_url}] {text}',
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f'ffmpeg stderr reader failed: {exc}', file=sys.stderr)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()


def main():
    if not BOARD_TOKEN or BOARD_TOKEN == 'dev-board-token':
        raise SystemExit('BOARD_TOKEN must be configured with a non-default value')

    register_device()

    preferred_video_device, auto_video_select = parse_video_device_setting(VIDEO_DEVICE_SETTING)
    active_video_device = resolve_working_video_device(preferred_video_device)
    print(
        f'video device strategy setting={VIDEO_DEVICE_SETTING} auto_select={auto_video_select} '
        f'auto_recover={VIDEO_DEVICE_AUTO_RECOVER} active_device={active_video_device}'
    )

    publisher_process = None
    active_publish_url = None
    stream_enabled = False
    stream_disabled_since = None
    next_probe_at = 0.0

    while True:
        config = fetch_stream_config()
        if config is not None:
            stream_enabled = bool(config.get('enabled'))
            cfg_board_name = (config.get('board_name') or '').strip()
            configured_rtsp_url = (config.get('rtsp_url') or '').strip()
            publish_url = PUBLISH_RTSP_URL or configured_rtsp_url
            expected_suffix = f'/cars/{urllib_parse.quote(BOARD_NAME, safe="")}'
            if stream_enabled:
                if cfg_board_name and cfg_board_name != BOARD_NAME:
                    print(
                        f'stream config board mismatch expected={BOARD_NAME} got={cfg_board_name}',
                        file=sys.stderr,
                    )
                if configured_rtsp_url and not configured_rtsp_url.endswith(expected_suffix):
                    print(
                        f'stream config RTSP mismatch expected suffix={expected_suffix} got={configured_rtsp_url}',
                        file=sys.stderr,
                    )
        else:
            publish_url = active_publish_url

        if stream_enabled:
            stream_disabled_since = None
        elif stream_disabled_since is None:
            stream_disabled_since = time.monotonic()

        if stream_enabled and publish_url:
            now = time.monotonic()
            # A running FFmpeg process owns the V4L2 device. Probing it here can
            # return EBUSY and incorrectly mark a healthy camera as unavailable.
            should_probe = publisher_process is None and (
                active_video_device is None or auto_video_select or VIDEO_DEVICE_AUTO_RECOVER
            )
            if should_probe and now >= next_probe_at:
                detected = resolve_working_video_device(active_video_device)
                if detected != active_video_device:
                    print(f'switching video device from {active_video_device} to {detected}')
                    active_video_device = detected
                next_probe_at = now + VIDEO_REPROBE_DELAY_SECONDS

            if active_video_device is None:
                print('no working camera detected for MediaMTX publish; retrying', file=sys.stderr)
                time.sleep(STREAM_STATUS_POLL_SECONDS)
                continue

            if publisher_process is None or active_publish_url != publish_url:
                stop_publisher(publisher_process, 'reconfigure')
                camera_mode = resolve_camera_mode(active_video_device)
                publisher_process = start_publisher(active_video_device, publish_url, camera_mode)
                stream_ffmpeg_stderr(publisher_process, active_video_device, publish_url)
                active_publish_url = publish_url

            if publisher_process.poll() is not None:
                stderr_tail = ''
                if publisher_process.stderr is not None:
                    try:
                        stderr_tail = (publisher_process.stderr.read() or '').strip()
                    except Exception:
                        stderr_tail = ''
                print(
                    f'MediaMTX publisher exited code={publisher_process.returncode} '
                    f'stderr={stderr_tail or "n/a"}',
                    file=sys.stderr,
                )
                stop_publisher(publisher_process, 'unexpected-exit')
                publisher_process = None
                time.sleep(0.5)

        else:
            if publisher_process is not None and stream_disabled_since is not None:
                disabled_for = time.monotonic() - stream_disabled_since
                if disabled_for >= STREAM_DISABLE_GRACE_SECONDS:
                    stop_publisher(publisher_process, 'disabled')
                    publisher_process = None
                    active_publish_url = None
                    print('MediaMTX publisher stopped while stream disabled')

        time.sleep(STREAM_STATUS_POLL_SECONDS)


if __name__ == '__main__':
    main()
