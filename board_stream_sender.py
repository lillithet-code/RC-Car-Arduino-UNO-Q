#!/usr/bin/env python3
import os
import sys
import time
from urllib import request as urllib_request

import cv2


SERVER_BASE_URL = os.environ.get('SERVER_URL', 'https://drive.kbob.org').rstrip('/')
STREAM_ENDPOINT = f'{SERVER_BASE_URL}/api/video/pipeline/frame'
BOARD_TOKEN = os.environ.get('BOARD_TOKEN', 'dev-board-token')
VIDEO_DEVICE = int(os.environ.get('VIDEO_DEVICE', '0'))
FRAME_RATE = float(os.environ.get('FRAME_RATE', '20'))
QUALITY = int(os.environ.get('JPEG_QUALITY', '70'))
UPLOAD_TIMEOUT_SECONDS = float(os.environ.get('UPLOAD_TIMEOUT_SECONDS', '5.0'))
STREAM_MODE = os.environ.get('STREAM_MODE', 'hardware').strip().lower()
VIDEO_WIDTH = int(os.environ.get('VIDEO_WIDTH', '1280'))
VIDEO_HEIGHT = int(os.environ.get('VIDEO_HEIGHT', '720'))
CAMERA_FOURCC = os.environ.get('CAMERA_FOURCC', 'MJPG').strip().upper()
CAPTURE_BUFFER_SIZE = int(os.environ.get('CAPTURE_BUFFER_SIZE', '1'))


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


def main():
    cap = open_capture(VIDEO_DEVICE)
    if not cap.isOpened():
        print('Unable to open camera device', file=sys.stderr)
        sys.exit(1)

    frame_period = 1.0 / max(FRAME_RATE, 1)

    while True:
        frame_started = time.monotonic()
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.01)
            continue

        _, encoded = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), QUALITY])
        payload = encoded.tobytes()
        req = urllib_request.Request(
            STREAM_ENDPOINT,
            data=payload,
            headers={'Content-Type': 'image/jpeg', 'X-Board-Token': BOARD_TOKEN},
            method='POST',
        )
        try:
            urllib_request.urlopen(req, timeout=max(UPLOAD_TIMEOUT_SECONDS, 1.0))
        except Exception as exc:
            print(f'Upload failed: {exc}', file=sys.stderr)

        elapsed = time.monotonic() - frame_started
        remaining = frame_period - elapsed
        if remaining > 0:
            time.sleep(remaining)


if __name__ == '__main__':
    main()
