#!/usr/bin/env python3
import json
import http.client
import glob
import os
import re
import select
import subprocess
import sys
import time
import ssl
import socket
from urllib import request as urllib_request
from urllib import parse as urllib_parse

import cv2


SERVER_BASE_URL = os.environ.get('SERVER_URL', 'https://drive.kbob.org').rstrip('/')
SERVER_BASE_PARTS = urllib_parse.urlparse(SERVER_BASE_URL)
SERVER_SCHEME = SERVER_BASE_PARTS.scheme or 'https'
SERVER_HOST = SERVER_BASE_PARTS.hostname or SERVER_BASE_PARTS.path
SERVER_PORT = SERVER_BASE_PARTS.port or (443 if SERVER_SCHEME == 'https' else 80)
STREAM_ENDPOINT = f'{SERVER_BASE_URL}/api/video/pipeline/frame'
STREAM_H264_ENDPOINT = f'{SERVER_BASE_URL}/api/video/h264/chunk'
STREAM_STATUS_ENDPOINT = f'{SERVER_BASE_URL}/api/board/stream/status'
BOARD_TOKEN = os.environ.get('BOARD_TOKEN', 'dev-board-token')
BOARD_NAME = os.environ.get('BOARD_NAME', socket.gethostname() or 'rc-car-1')
VIDEO_DEVICE_SETTING = os.environ.get('VIDEO_DEVICE', 'auto').strip().lower()
VIDEO_DEVICE_AUTO_RECOVER = os.environ.get('VIDEO_DEVICE_AUTO_RECOVER', '1').strip().lower() not in {'0', 'false', 'no', 'off'}
FRAME_RATE = float(os.environ.get('FRAME_RATE', '25'))
QUALITY = int(os.environ.get('JPEG_QUALITY', '70'))
UPLOAD_TIMEOUT_SECONDS = float(os.environ.get('UPLOAD_TIMEOUT_SECONDS', '5.0'))
STREAM_MODE = os.environ.get('STREAM_MODE', 'hardware').strip().lower()
VIDEO_WIDTH = int(os.environ.get('VIDEO_WIDTH', '1280'))
VIDEO_HEIGHT = int(os.environ.get('VIDEO_HEIGHT', '720'))
CAMERA_FOURCC = os.environ.get('CAMERA_FOURCC', 'MJPG').strip().upper()
CAPTURE_BUFFER_SIZE = int(os.environ.get('CAPTURE_BUFFER_SIZE', '1'))
STREAM_IDLE_POLL_SECONDS = float(os.environ.get('STREAM_IDLE_POLL_SECONDS', '2.0'))
STREAM_PIPELINE = os.environ.get('STREAM_PIPELINE', 'auto').strip().lower()
FFMPEG_BIN = os.environ.get('FFMPEG_BIN', 'ffmpeg').strip()
H264_BITRATE = os.environ.get('H264_BITRATE', '2500k').strip()
H264_MAXRATE = os.environ.get('H264_MAXRATE', H264_BITRATE).strip()
H264_BUFSIZE = os.environ.get('H264_BUFSIZE', '1500k').strip()
H264_GOP = int(os.environ.get('H264_GOP', str(max(1, int(FRAME_RATE)))) )
H264_CHUNK_BYTES = int(os.environ.get('H264_CHUNK_BYTES', '8192'))
H264_INPUT_FORMATS = os.environ.get('H264_INPUT_FORMATS', '').strip()
H264_ENCODER_MODE = os.environ.get('H264_ENCODER_MODE', 'auto').strip().lower()
H264_ENCODERS = os.environ.get('H264_ENCODERS', 'h264_v4l2m2m,h264_omx').strip()
H264_UPLOAD_BATCH_BYTES = int(os.environ.get('H264_UPLOAD_BATCH_BYTES', '65536'))
H264_UPLOAD_MAX_DELAY_MS = int(os.environ.get('H264_UPLOAD_MAX_DELAY_MS', '80'))
STREAM_PROFILE = os.environ.get('STREAM_PROFILE', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
STREAM_PROFILE_EVERY = max(1, int(os.environ.get('STREAM_PROFILE_EVERY', '30')))
STREAM_PROFILE_HEARTBEAT_POLLS = max(1, int(os.environ.get('STREAM_PROFILE_HEARTBEAT_POLLS', '20')))
_UPLOAD_CONNECTION = None
_UPLOAD_CONNECTION_KEY = None

INPUT_FORMAT_ALIASES = {
    'mjpg': 'mjpeg',
    'mjpeg': 'mjpeg',
    'yuyv': 'yuyv422',
    'yuyv422': 'yuyv422',
    'uyvy': 'uyvy422',
    'uyvy422': 'uyvy422',
}


def normalize_input_format(value):
    raw = (value or '').strip().lower()
    return INPUT_FORMAT_ALIASES.get(raw, raw)


def get_available_h264_encoders():
    try:
        result = subprocess.run(
            [FFMPEG_BIN, '-hide_banner', '-encoders'],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output = result.stdout or ''
    except Exception:
        return set()

    available = set()
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith('Encoders:'):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        encoder_name = parts[1].strip()
        if encoder_name.startswith('h264_'):
            available.add(encoder_name)
    return available


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
        if not match:
            continue
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

    return preferred_index


def get_h264_encoders():
    values = [item.strip() for item in H264_ENCODERS.split(',') if item.strip()]
    if not values:
        return ['h264_v4l2m2m']

    available = get_available_h264_encoders()
    if not available:
        return values

    filtered = [encoder for encoder in values if encoder in available]
    if filtered:
        return filtered
    return values


def get_h264_input_formats():
    if H264_INPUT_FORMATS:
        values = [normalize_input_format(item) for item in H264_INPUT_FORMATS.split(',') if item.strip()]
        if values:
            return values

    defaults = [normalize_input_format(CAMERA_FOURCC), 'yuyv422', 'uyvy422']
    ordered = []
    for item in defaults:
        if item not in ordered:
            ordered.append(item)
    return ordered


def open_capture(device_index):
    backend = cv2.CAP_V4L2 if hasattr(cv2, 'CAP_V4L2') else 0
    cap = cv2.VideoCapture(device_index, backend)
    if not cap.isOpened():
        return cap

    # Ask camera for low-latency V4L2 settings first.
    cap.set(cv2.CAP_PROP_BUFFERSIZE, max(1, CAPTURE_BUFFER_SIZE))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FRAME_RATE)

    if STREAM_MODE in {'hardware', 'auto'}:
        fourcc = cv2.VideoWriter_fourcc(*CAMERA_FOURCC)
        cap.set(cv2.CAP_PROP_FOURCC, fourcc)

    actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    actual_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
    actual_fourcc_raw = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
    actual_fourcc = ''.join(chr((actual_fourcc_raw >> 8 * i) & 0xFF) for i in range(4)).strip() or '????'
    print(
        f'capture configured mode={STREAM_MODE} device={device_index} '
        f'{actual_width}x{actual_height}@{actual_fps:.2f} fourcc={actual_fourcc}'
    )

    return cap


def fetch_stream_enabled():
    board_name = urllib_parse.quote(BOARD_NAME, safe='')
    url = f'{STREAM_STATUS_ENDPOINT}?token={BOARD_TOKEN}&board_name={board_name}'
    req = urllib_request.Request(url, headers={'User-Agent': 'RC-Car-Board-Stream/1.0'})
    try:
        with urllib_request.urlopen(req, timeout=max(UPLOAD_TIMEOUT_SECONDS, 1.0)) as response:
            payload = json.loads(response.read().decode('utf-8'))
        return bool(payload.get('enabled', False))
    except Exception as exc:
        print(f'stream status check failed: {exc}', file=sys.stderr)
        return False


def fetch_stream_enabled_profiled(poll_count):
    enabled = fetch_stream_enabled()
    if STREAM_PROFILE and (poll_count <= 3 or poll_count % STREAM_PROFILE_HEARTBEAT_POLLS == 0):
        print(f'stream profile gate poll={poll_count} enabled={enabled}')
    return enabled


def post_bytes(url, payload, content_type):
    global _UPLOAD_CONNECTION, _UPLOAD_CONNECTION_KEY

    parsed = urllib_parse.urlparse(url)
    request_path = parsed.path or '/'
    if parsed.query:
        request_path = f'{request_path}?{parsed.query}'

    connection_key = (parsed.scheme or SERVER_SCHEME, parsed.hostname or SERVER_HOST, parsed.port or SERVER_PORT)
    if _UPLOAD_CONNECTION is None or _UPLOAD_CONNECTION_KEY != connection_key:
        if _UPLOAD_CONNECTION is not None:
            try:
                _UPLOAD_CONNECTION.close()
            except Exception:
                pass
        timeout = max(UPLOAD_TIMEOUT_SECONDS, 1.0)
        if connection_key[0] == 'https':
            _UPLOAD_CONNECTION = http.client.HTTPSConnection(
                connection_key[1],
                connection_key[2],
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            _UPLOAD_CONNECTION = http.client.HTTPConnection(
                connection_key[1],
                connection_key[2],
                timeout=timeout,
            )
        _UPLOAD_CONNECTION_KEY = connection_key

    headers = {
        'Content-Type': content_type,
        'Content-Length': str(len(payload)),
        'Connection': 'keep-alive',
        'X-Board-Token': BOARD_TOKEN,
        'X-Board-Name': BOARD_NAME,
    }

    try:
        _UPLOAD_CONNECTION.request('POST', request_path, body=payload, headers=headers)
        response = _UPLOAD_CONNECTION.getresponse()
        response.read()
        if response.status >= 400:
            raise RuntimeError(f'upload returned HTTP {response.status}')
    except Exception:
        try:
            _UPLOAD_CONNECTION.close()
        except Exception:
            pass
        _UPLOAD_CONNECTION = None
        _UPLOAD_CONNECTION_KEY = None
        raise


def log_profile_summary(*, path, event_count, total_elapsed, read_elapsed=None, post_elapsed=None, encode_elapsed=None, upload_bytes=None, extra=None):
    if not STREAM_PROFILE or event_count <= 0:
        return

    rate = event_count / max(total_elapsed, 1e-6)
    parts = [f'stream profile path={path}', f'events={event_count}', f'rate={rate:.2f}', f'total_ms={total_elapsed * 1000:.1f}']
    if read_elapsed is not None:
        parts.append(f'read_ms={read_elapsed * 1000:.1f}')
    if encode_elapsed is not None:
        parts.append(f'encode_ms={encode_elapsed * 1000:.1f}')
    if post_elapsed is not None:
        parts.append(f'post_ms={post_elapsed * 1000:.1f}')
    if upload_bytes is not None:
        parts.append(f'bytes={upload_bytes}')
    if extra:
        parts.append(extra)
    print(' '.join(parts))


def log_profile_event(*, path, message):
    if STREAM_PROFILE:
        print(f'stream profile path={path} {message}')


def start_h264_ffmpeg(input_format, encoder_name, video_device_index):
    encoder_mode = H264_ENCODER_MODE
    if encoder_mode not in {'auto', 'copy', 'transcode'}:
        encoder_mode = 'auto'

    use_copy = encoder_mode == 'copy' or (encoder_mode == 'auto' and input_format == 'h264')

    command = [
        FFMPEG_BIN,
        '-hide_banner',
        '-loglevel', 'error',
        '-fflags', 'nobuffer',
        '-f', 'v4l2',
        '-input_format', input_format,
        '-video_size', f'{VIDEO_WIDTH}x{VIDEO_HEIGHT}',
        '-framerate', str(FRAME_RATE),
        '-i', f'/dev/video{video_device_index}',
        '-an',
    ]

    if use_copy:
        command.extend([
            '-c:v', 'copy',
            '-f', 'h264',
            '-',
        ])
    else:
        command.extend([
            '-vf', 'format=nv12',
            '-c:v', encoder_name,
            '-pix_fmt', 'nv12',
            '-g', str(max(1, H264_GOP)),
            '-bf', '0',
            '-b:v', H264_BITRATE,
            '-maxrate', H264_MAXRATE,
            '-bufsize', H264_BUFSIZE,
            '-f', 'h264',
            '-',
        ])

    mode_label = 'copy' if use_copy else 'transcode'
    print(
        f'starting h264 pipeline mode={mode_label} encoder={encoder_name} '
        f'input_format={input_format}: {" ".join(command)}'
    )
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def main():
    cap = None
    h264_process = None
    frame_period = 1.0 / max(FRAME_RATE, 1)
    profile_event_count = 0
    profile_window_started = time.monotonic()
    profile_read_elapsed = 0.0
    profile_post_elapsed = 0.0
    profile_encode_elapsed = 0.0
    profile_upload_bytes = 0
    last_enabled = None
    h264_input_formats = get_h264_input_formats()
    h264_input_index = 0
    h264_encoders = get_h264_encoders()
    h264_encoder_index = 0
    selected_pipeline = STREAM_PIPELINE
    if selected_pipeline == 'auto':
        selected_pipeline = 'h264_hw'
    if selected_pipeline not in {'h264_hw', 'h264_hw_strict', 'mjpg'}:
        selected_pipeline = 'mjpg'
    strict_hardware_only = selected_pipeline == 'h264_hw_strict'
    preferred_video_device, auto_video_select = parse_video_device_setting(VIDEO_DEVICE_SETTING)
    active_video_device = resolve_working_video_device(preferred_video_device)
    if active_video_device is None:
        active_video_device = 0
    print(
        f'video device strategy: setting={VIDEO_DEVICE_SETTING} auto_select={auto_video_select} '
        f'auto_recover={VIDEO_DEVICE_AUTO_RECOVER} active_device={active_video_device}'
    )
    print(
        f'selected stream pipeline: {selected_pipeline} '
        f'h264_input_formats={"|".join(h264_input_formats)} '
        f'h264_encoder_mode={H264_ENCODER_MODE} '
        f'h264_encoders={"|".join(h264_encoders)}'
    )
    if selected_pipeline in {'h264_hw', 'h264_hw_strict'}:
        print('stream profile path=h264_hw hardware_only_fallback=disabled')
    if STREAM_PROFILE:
        print(f'stream profiling enabled every={STREAM_PROFILE_EVERY}')
        print(f'stream profiling heartbeat polls={STREAM_PROFILE_HEARTBEAT_POLLS}')
    h264_failures = 0
    h264_upload_buffer = bytearray()
    h264_last_upload = time.monotonic()
    h264_batch_bytes = max(1024, H264_UPLOAD_BATCH_BYTES)
    h264_max_delay = max(5, H264_UPLOAD_MAX_DELAY_MS) / 1000.0
    poll_count = 0

    while True:
        poll_count += 1
        stream_enabled = fetch_stream_enabled_profiled(poll_count)

        if stream_enabled != last_enabled:
            state_text = 'enabled' if stream_enabled else 'disabled'
            print(f'stream state changed: {state_text}')
            last_enabled = stream_enabled

        if not stream_enabled:
            if cap is not None:
                cap.release()
                cap = None
                print('camera released while idle')
            if h264_process is not None:
                h264_process.terminate()
                h264_process = None
                print('h264 encoder stopped while idle')
            if h264_upload_buffer:
                h264_upload_buffer.clear()
            if STREAM_PROFILE and (poll_count <= 3 or poll_count % STREAM_PROFILE_HEARTBEAT_POLLS == 0):
                log_profile_event(path=selected_pipeline, message='idle waiting_for_session')
            time.sleep(max(STREAM_IDLE_POLL_SECONDS, 0.2))
            continue

        if selected_pipeline in {'h264_hw', 'h264_hw_strict'}:
            if h264_process is None:
                try:
                    if auto_video_select or VIDEO_DEVICE_AUTO_RECOVER:
                        detected_device = resolve_working_video_device(active_video_device)
                        if detected_device != active_video_device:
                            print(f'switching video device from {active_video_device} to {detected_device}')
                            active_video_device = detected_device
                    active_input = h264_input_formats[h264_input_index]
                    active_encoder = h264_encoders[h264_encoder_index]
                    h264_process = start_h264_ffmpeg(active_input, active_encoder, active_video_device)
                except Exception as exc:
                    if strict_hardware_only:
                        print(f'h264 pipeline start failed in strict mode; retrying: {exc}', file=sys.stderr)
                        time.sleep(0.5)
                        continue
                    print(f'h264 pipeline start failed; staying on hardware path: {exc}', file=sys.stderr)
                    continue

            if h264_process.poll() is not None:
                h264_failures += 1
                exit_code = h264_process.returncode
                stderr_output = ''
                try:
                    if h264_process.stderr is not None:
                        stderr_output = h264_process.stderr.read().decode('utf-8', errors='ignore').strip()
                except Exception:
                    stderr_output = ''

                if stderr_output:
                    print(f'h264 encoder exited; stderr: {stderr_output}', file=sys.stderr)
                else:
                    print('h264 encoder exited; restarting', file=sys.stderr)

                normalized_error = stderr_output.lower()
                if VIDEO_DEVICE_AUTO_RECOVER and any(
                    marker in normalized_error
                    for marker in ('not a video capture device', 'no such device', 'error opening input')
                ):
                    recovered_device = resolve_working_video_device(preferred_video_device)
                    if recovered_device != active_video_device:
                        print(f'camera node changed; switching video device from {active_video_device} to {recovered_device}', file=sys.stderr)
                        active_video_device = recovered_device

                h264_process = None
                if h264_upload_buffer:
                    h264_upload_buffer.clear()
                if h264_input_formats:
                    h264_input_index = (h264_input_index + 1) % len(h264_input_formats)
                    print(
                        f'rotating h264 input format to {h264_input_formats[h264_input_index]}',
                        file=sys.stderr,
                    )
                if h264_encoders:
                    h264_encoder_index = (h264_encoder_index + 1) % len(h264_encoders)
                    print(
                        f'rotating h264 encoder to {h264_encoders[h264_encoder_index]}',
                        file=sys.stderr,
                    )
                if exit_code == -11:
                    if strict_hardware_only:
                        print('h264 encoder crashed with segmentation fault; strict mode keeps hardware-only retries', file=sys.stderr)
                    else:
                        print('h264 encoder crashed with segmentation fault; staying on hardware path', file=sys.stderr)
                elif h264_failures >= 5:
                    if strict_hardware_only:
                        print('h264 encoder repeatedly failed; strict mode keeps hardware-only retries', file=sys.stderr)
                    else:
                        print('h264 encoder repeatedly failed; staying on hardware path', file=sys.stderr)
                time.sleep(0.2)
                continue

            ready, _, _ = select.select([h264_process.stdout], [], [], 0.5)
            if not ready:
                if h264_upload_buffer and (time.monotonic() - h264_last_upload) >= h264_max_delay:
                    flush_started = time.monotonic()
                    flush_size = len(h264_upload_buffer)
                    try:
                        post_bytes(STREAM_H264_ENDPOINT, bytes(h264_upload_buffer), 'video/h264')
                        h264_upload_buffer.clear()
                        h264_last_upload = time.monotonic()
                        h264_failures = 0
                        if STREAM_PROFILE:
                            profile_post_elapsed += time.monotonic() - flush_started
                            profile_upload_bytes += flush_size
                            profile_event_count += 1
                            log_profile_event(
                                path='h264_hw',
                                message=f'flush bytes={flush_size} queued={profile_upload_bytes} events={profile_event_count}',
                            )
                    except Exception as exc:
                        print(f'H264 upload failed: {exc}', file=sys.stderr)
                    if STREAM_PROFILE and (profile_event_count <= 3 or profile_event_count % STREAM_PROFILE_EVERY == 0):
                        profile_total_elapsed = time.monotonic() - profile_window_started
                        log_profile_summary(
                            path='h264_hw',
                            event_count=profile_event_count,
                            total_elapsed=profile_total_elapsed,
                            read_elapsed=profile_read_elapsed,
                            post_elapsed=profile_post_elapsed,
                            upload_bytes=profile_upload_bytes,
                        )
                        profile_event_count = 0
                        profile_window_started = time.monotonic()
                        profile_read_elapsed = 0.0
                        profile_post_elapsed = 0.0
                        profile_upload_bytes = 0
                continue

            read_started = time.monotonic()
            chunk = h264_process.stdout.read(max(1024, H264_CHUNK_BYTES))
            profile_read_elapsed += time.monotonic() - read_started
            if not chunk:
                time.sleep(0.05)
                continue

            h264_upload_buffer.extend(chunk)
            should_flush = (
                len(h264_upload_buffer) >= h264_batch_bytes
                or (time.monotonic() - h264_last_upload) >= h264_max_delay
            )

            if not should_flush:
                continue

            post_started = time.monotonic()
            try:
                flush_size = len(h264_upload_buffer)
                post_bytes(STREAM_H264_ENDPOINT, bytes(h264_upload_buffer), 'video/h264')
                h264_upload_buffer.clear()
                h264_last_upload = time.monotonic()
                h264_failures = 0
                if STREAM_PROFILE:
                    profile_post_elapsed += time.monotonic() - post_started
                    profile_upload_bytes += flush_size
                    profile_event_count += 1
                    log_profile_event(
                        path='h264_hw',
                        message=f'flush bytes={flush_size} queued={profile_upload_bytes} events={profile_event_count}',
                    )
            except Exception as exc:
                print(f'H264 upload failed: {exc}', file=sys.stderr)

            if STREAM_PROFILE and (profile_event_count <= 3 or profile_event_count % STREAM_PROFILE_EVERY == 0):
                profile_total_elapsed = time.monotonic() - profile_window_started
                log_profile_summary(
                    path='h264_hw',
                    event_count=profile_event_count,
                    total_elapsed=profile_total_elapsed,
                    read_elapsed=profile_read_elapsed,
                    post_elapsed=profile_post_elapsed,
                    upload_bytes=profile_upload_bytes,
                )
                profile_event_count = 0
                profile_window_started = time.monotonic()
                profile_read_elapsed = 0.0
                profile_post_elapsed = 0.0
                profile_upload_bytes = 0
            continue

        if cap is None:
            if auto_video_select or VIDEO_DEVICE_AUTO_RECOVER:
                detected_device = resolve_working_video_device(active_video_device)
                if detected_device != active_video_device:
                    print(f'switching video device from {active_video_device} to {detected_device}')
                    active_video_device = detected_device

            cap = open_capture(active_video_device)
            if not cap.isOpened():
                print(f'Unable to open camera device /dev/video{active_video_device}', file=sys.stderr)
                cap = None
                time.sleep(max(STREAM_IDLE_POLL_SECONDS, 0.2))
                continue

        frame_started = time.monotonic()
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.01)
            continue

        encode_started = time.monotonic()
        _, encoded = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), QUALITY])
        encode_elapsed = time.monotonic() - encode_started
        payload = encoded.tobytes()
        post_started = time.monotonic()
        try:
            post_bytes(STREAM_ENDPOINT, payload, 'image/jpeg')
        except Exception as exc:
            print(f'Upload failed: {exc}', file=sys.stderr)

        if STREAM_PROFILE:
            profile_event_count += 1
            profile_read_elapsed += encode_started - frame_started
            profile_encode_elapsed += encode_elapsed
            profile_post_elapsed += time.monotonic() - post_started
            profile_upload_bytes += len(payload)
            log_profile_event(
                path='mjpg',
                message=f'frame bytes={len(payload)} events={profile_event_count} encode_ms={encode_elapsed * 1000:.1f}',
            )
            if profile_event_count <= 3 or profile_event_count % STREAM_PROFILE_EVERY == 0:
                profile_total_elapsed = time.monotonic() - profile_window_started
                log_profile_summary(
                    path='mjpg',
                    event_count=profile_event_count,
                    total_elapsed=profile_total_elapsed,
                    read_elapsed=profile_read_elapsed,
                    post_elapsed=profile_post_elapsed,
                    encode_elapsed=profile_encode_elapsed,
                    upload_bytes=profile_upload_bytes,
                )
                profile_event_count = 0
                profile_window_started = time.monotonic()
                profile_read_elapsed = 0.0
                profile_post_elapsed = 0.0
                profile_encode_elapsed = 0.0
                profile_upload_bytes = 0

        elapsed = time.monotonic() - frame_started
        remaining = frame_period - elapsed
        if remaining > 0:
            time.sleep(remaining)


if __name__ == '__main__':
    main()
