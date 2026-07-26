#!/usr/bin/env python3
import json
import os
import select
import subprocess
import sys
import time
from urllib import request as urllib_request

import cv2


SERVER_BASE_URL = os.environ.get('SERVER_URL', 'https://drive.kbob.org').rstrip('/')
STREAM_ENDPOINT = f'{SERVER_BASE_URL}/api/video/pipeline/frame'
STREAM_H264_ENDPOINT = f'{SERVER_BASE_URL}/api/video/h264/chunk'
STREAM_STATUS_ENDPOINT = f'{SERVER_BASE_URL}/api/board/stream/status'
BOARD_TOKEN = os.environ.get('BOARD_TOKEN', 'dev-board-token')
VIDEO_DEVICE = int(os.environ.get('VIDEO_DEVICE', '0'))
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


def get_h264_input_formats():
    if H264_INPUT_FORMATS:
        values = [item.strip().lower() for item in H264_INPUT_FORMATS.split(',') if item.strip()]
        if values:
            return values

    defaults = [CAMERA_FOURCC.lower(), 'yuyv422', 'uyvy422']
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
    url = f'{STREAM_STATUS_ENDPOINT}?token={BOARD_TOKEN}'
    req = urllib_request.Request(url, headers={'User-Agent': 'RC-Car-Board-Stream/1.0'})
    try:
        with urllib_request.urlopen(req, timeout=max(UPLOAD_TIMEOUT_SECONDS, 1.0)) as response:
            payload = json.loads(response.read().decode('utf-8'))
        return bool(payload.get('enabled', False))
    except Exception as exc:
        print(f'stream status check failed: {exc}', file=sys.stderr)
        return False


def post_bytes(url, payload, content_type):
    req = urllib_request.Request(
        url,
        data=payload,
        headers={
            'Content-Type': content_type,
            'X-Board-Token': BOARD_TOKEN,
        },
        method='POST',
    )
    urllib_request.urlopen(req, timeout=max(UPLOAD_TIMEOUT_SECONDS, 1.0))


def start_h264_ffmpeg(input_format):
    command = [
        FFMPEG_BIN,
        '-hide_banner',
        '-loglevel', 'error',
        '-fflags', 'nobuffer',
        '-f', 'v4l2',
        '-input_format', input_format,
        '-video_size', f'{VIDEO_WIDTH}x{VIDEO_HEIGHT}',
        '-framerate', str(FRAME_RATE),
        '-i', f'/dev/video{VIDEO_DEVICE}',
        '-an',
        '-vf', 'format=nv12',
        '-c:v', 'h264_v4l2m2m',
        '-pix_fmt', 'nv12',
        '-g', str(max(1, H264_GOP)),
        '-bf', '0',
        '-b:v', H264_BITRATE,
        '-maxrate', H264_MAXRATE,
        '-bufsize', H264_BUFSIZE,
        '-f', 'h264',
        '-',
    ]

    print(f'starting h264 pipeline input_format={input_format}: {" ".join(command)}')
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
    last_enabled = None
    h264_input_formats = get_h264_input_formats()
    h264_input_index = 0
    selected_pipeline = STREAM_PIPELINE
    if selected_pipeline == 'auto':
        selected_pipeline = 'h264_hw'
    if selected_pipeline not in {'h264_hw', 'mjpg'}:
        selected_pipeline = 'mjpg'
    print(
        f'selected stream pipeline: {selected_pipeline} '
        f'h264_input_formats={"|".join(h264_input_formats)}'
    )
    h264_failures = 0

    while True:
        stream_enabled = fetch_stream_enabled()

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
            time.sleep(max(STREAM_IDLE_POLL_SECONDS, 0.2))
            continue

        if selected_pipeline == 'h264_hw':
            if h264_process is None:
                try:
                    active_input = h264_input_formats[h264_input_index]
                    h264_process = start_h264_ffmpeg(active_input)
                    h264_failures = 0
                except Exception as exc:
                    print(f'h264 pipeline start failed, fallback to mjpg: {exc}', file=sys.stderr)
                    selected_pipeline = 'mjpg'
                    continue

            if h264_process.poll() is not None:
                h264_failures += 1
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

                h264_process = None
                if h264_input_formats:
                    h264_input_index = (h264_input_index + 1) % len(h264_input_formats)
                    print(
                        f'rotating h264 input format to {h264_input_formats[h264_input_index]}',
                        file=sys.stderr,
                    )
                if h264_failures >= 5:
                    print('h264 encoder repeatedly failed; falling back to mjpg pipeline', file=sys.stderr)
                    selected_pipeline = 'mjpg'
                time.sleep(0.2)
                continue

            ready, _, _ = select.select([h264_process.stdout], [], [], 0.5)
            if not ready:
                continue

            chunk = h264_process.stdout.read(max(1024, H264_CHUNK_BYTES))
            if not chunk:
                time.sleep(0.05)
                continue

            try:
                post_bytes(STREAM_H264_ENDPOINT, chunk, 'video/h264')
            except Exception as exc:
                print(f'H264 upload failed: {exc}', file=sys.stderr)
            continue

        if cap is None:
            cap = open_capture(VIDEO_DEVICE)
            if not cap.isOpened():
                print('Unable to open camera device', file=sys.stderr)
                cap = None
                time.sleep(max(STREAM_IDLE_POLL_SECONDS, 0.2))
                continue

        frame_started = time.monotonic()
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.01)
            continue

        _, encoded = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), QUALITY])
        payload = encoded.tobytes()
        try:
            post_bytes(STREAM_ENDPOINT, payload, 'image/jpeg')
        except Exception as exc:
            print(f'Upload failed: {exc}', file=sys.stderr)

        elapsed = time.monotonic() - frame_started
        remaining = frame_period - elapsed
        if remaining > 0:
            time.sleep(remaining)


if __name__ == '__main__':
    main()
