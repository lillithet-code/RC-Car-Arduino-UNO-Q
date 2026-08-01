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
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import cv2


SERVER_BASE_URL = os.environ.get('SERVER_URL', 'https://drive.kbob.org').rstrip('/')
BOARD_TOKEN = os.environ.get('BOARD_TOKEN', 'dev-board-token')
BOARD_NAME = os.environ.get('BOARD_NAME', socket.gethostname() or 'rc-car-1')
BOARD_LOCATION = os.environ.get('BOARD_LOCATION', 'unknown')
VIDEO_DEVICE_SETTING = os.environ.get('VIDEO_DEVICE', 'auto').strip().lower()
VIDEO_DEVICE_AUTO_RECOVER = os.environ.get('VIDEO_DEVICE_AUTO_RECOVER', '1').strip().lower() not in {'0', 'false', 'no', 'off'}
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


def resolve_input_format():
    direct = (os.environ.get('H264_INPUT_FORMAT') or '').strip().lower()
    if direct:
        return direct

    # Backward compatibility with installer/env files that still use the plural key.
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
            indices.append(int(match.group(1)))
    return indices


def probe_video_device(device_index):
    backend = cv2.CAP_V4L2 if hasattr(cv2, 'CAP_V4L2') else 0
    cap = cv2.VideoCapture(device_index, backend)
    if not cap.isOpened():
        cap.release()
        return False

    ok = False
    for _ in range(12):
        ret, _ = cap.read()
        if ret:
            ok = True
            break
        time.sleep(0.05)

    cap.release()
    return ok


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
        headers={'Content-Type': 'application/json', 'User-Agent': 'RC-Car-Board-Stream/2.0'},
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
    token = urllib_parse.quote(BOARD_TOKEN, safe='')
    url = f'{STREAM_CONFIG_ENDPOINT}?token={token}&board_name={board_name}'
    req = urllib_request.Request(url, headers={'User-Agent': 'RC-Car-Board-Stream/2.0'})
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


def build_publish_command(video_device_index, rtsp_url):
    command = [
        FFMPEG_BIN,
        '-hide_banner',
        '-loglevel', 'error',
        '-fflags', 'nobuffer',
        '-f', 'v4l2',
        '-input_format', H264_INPUT_FORMAT,
        '-video_size', f'{VIDEO_WIDTH}x{VIDEO_HEIGHT}',
        '-framerate', str(FRAME_RATE),
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
        command.extend(['-preset', 'veryfast', '-tune', 'zerolatency'])

    command.extend([
        '-rtsp_transport', 'tcp',
        '-f', 'rtsp',
        rtsp_url,
    ])
    return command


def start_publisher(video_device_index, rtsp_url):
    command = build_publish_command(video_device_index, rtsp_url)
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


def v4l2_mode_probe(video_device_index):
    device = f'/dev/video{video_device_index}'
    if not os.path.exists(device):
        return

    try:
        result = subprocess.run(
            ['v4l2-ctl', '-d', device, '--list-formats-ext'],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except FileNotFoundError:
        print('v4l2-ctl not found; skipping camera capability probe', file=sys.stderr)
        return
    except Exception as exc:
        print(f'camera capability probe failed: {exc}', file=sys.stderr)
        return

    if result.returncode != 0:
        stderr_tail = (result.stderr or '').strip()
        print(f'v4l2-ctl probe failed for {device}: {stderr_tail or "unknown error"}', file=sys.stderr)
        return

    output = result.stdout or ''
    fmt_ok = H264_INPUT_FORMAT.lower() in output.lower()
    size_text = f'{VIDEO_WIDTH}x{VIDEO_HEIGHT}'
    size_ok = size_text in output
    fps_ok = f'{int(FRAME_RATE)}.000 fps' in output or f'{FRAME_RATE:.3f} fps' in output

    print(
        f'camera capability probe device={device} format={H264_INPUT_FORMAT} supported={fmt_ok} '
        f'resolution={size_text} supported={size_ok} fps={FRAME_RATE} supported={fps_ok}'
    )
    if not (fmt_ok and size_ok and fps_ok):
        print(
            f'configured mode may be unsupported on {device}; inspect v4l2-ctl output before changing quality',
            file=sys.stderr,
        )


def main():
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
            should_probe = active_video_device is None or auto_video_select or VIDEO_DEVICE_AUTO_RECOVER
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
                v4l2_mode_probe(active_video_device)
                publisher_process = start_publisher(active_video_device, publish_url)
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
