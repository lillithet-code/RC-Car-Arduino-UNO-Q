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


def test_resolve_requested_camera_mode_auto_detects_imx219(monkeypatch):
    monkeypatch.setattr(sender, 'discover_picamera_sensor', lambda: 'imx219')

    requested = sender.resolve_requested_camera_mode({'PICAMERA_SENSOR': 'auto'})
    assert requested.width == 1280
    assert requested.height == 720
    assert requested.fps == 30.0


def test_discover_picamera_sensor_detects_imx219(monkeypatch):
    monkeypatch.setattr(
        sender,
        '_list_camera_descriptions',
        lambda: [{'index': 0, 'description': '0: imx219 [imx219]'}],
    )

    assert sender.discover_picamera_sensor() == 'imx219'


def test_resolve_profile_from_config_ignores_server_override_for_imx219(monkeypatch):
    monkeypatch.setattr(sender, 'resolve_requested_camera_mode', lambda env=None, sensor_hint=None: sender.CameraMode('h264', 1280, 720, 30.0))

    payload = {
        'video_profile': {
            'video_mode': 'h264,1920,1080,60',
        }
    }

    requested = sender.resolve_profile_from_config(payload, sensor_hint='imx219')
    assert requested.width == 1280
    assert requested.height == 720
    assert requested.fps == 30.0


def test_resolve_profile_from_config_auto_detects_imx219_and_ignores_override(monkeypatch):
    monkeypatch.setattr(sender, 'discover_picamera_sensor', lambda: 'imx219')
    monkeypatch.setattr(sender, 'resolve_requested_camera_mode', lambda env=None, sensor_hint=None: sender.CameraMode('h264', 1280, 720, 30.0))

    payload = {
        'video_profile': {
            'video_mode': 'h264,1920,1080,60',
        }
    }

    requested = sender.resolve_profile_from_config(payload, sensor_hint='auto')
    assert requested.width == 1280
    assert requested.height == 720
    assert requested.fps == 30.0