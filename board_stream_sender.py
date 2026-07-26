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


def main():
    cap = cv2.VideoCapture(VIDEO_DEVICE)
    if not cap.isOpened():
        print('Unable to open camera device', file=sys.stderr)
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, FRAME_RATE)
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
