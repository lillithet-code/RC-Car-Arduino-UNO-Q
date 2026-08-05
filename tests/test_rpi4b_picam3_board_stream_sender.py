import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import rpi4b_picam3_board_stream_sender as sender


def test_resolve_requested_camera_mode_defaults_to_30fps_for_imx219():
    requested = sender.resolve_requested_camera_mode({'PICAMERA_SENSOR': 'imx219'})
    assert requested.width == 1280
    assert requested.height == 720
    assert requested.fps == 30.0


def test_resolve_requested_camera_mode_keeps_imx708_default_profile():
    requested = sender.resolve_requested_camera_mode({'PICAMERA_SENSOR': 'imx708'})
    assert requested.width == 1280
    assert requested.height == 720
    assert requested.fps == 60.0


def test_resolve_requested_camera_mode_ignores_generic_values_for_imx219():
    requested = sender.resolve_requested_camera_mode({
        'PICAMERA_SENSOR': 'imx219',
        'VIDEO_WIDTH': '1920',
        'VIDEO_HEIGHT': '1080',
        'FRAME_RATE': '60',
    })
    assert requested.width == 1280
    assert requested.height == 720
    assert requested.fps == 30.0