import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import rpi_csi_stream_sender as sender


def test_parse_mode_value_parses_expected_shape():
    mode = sender.parse_mode_value('h264,1280,720,30')
    assert mode is not None
    assert mode.input_format == 'h264'
    assert mode.width == 1280
    assert mode.height == 720
    assert mode.fps == 30.0


def test_select_best_mode_uses_closest_camera_mode():
    requested = sender.CameraMode(input_format='h264', width=1280, height=720, fps=30.0)
    camera = sender.CameraDescriptor(
        index=0,
        description='imx219 sample',
        modes=(
            sender.CameraMode(input_format='h264', width=640, height=480, fps=30.0),
            sender.CameraMode(input_format='h264', width=1280, height=720, fps=29.97),
            sender.CameraMode(input_format='h264', width=1920, height=1080, fps=30.0),
        ),
    )

    selected = sender.select_best_mode(requested, camera)
    assert selected.width == 1280
    assert selected.height == 720


def test_discover_cameras_uses_cache(monkeypatch):
    calls = {'count': 0}

    def fake_query():
        calls['count'] += 1
        return (
            sender.CameraDescriptor(
                index=0,
                description='imx708 [4608x2592]',
                modes=(sender.CameraMode(input_format='h264', width=1280, height=720, fps=30.0),),
            ),
        )

    monkeypatch.setattr(sender, '_query_list_cameras', fake_query)
    monkeypatch.setattr(sender, 'CAMERA_DISCOVERY_CACHE_SECONDS', 60.0)

    first = sender.discover_cameras(force_refresh=True)
    second = sender.discover_cameras(force_refresh=False)

    assert len(first) == 1
    assert len(second) == 1
    assert calls['count'] == 1


def test_resolve_selected_camera_honors_sensor_hint(monkeypatch):
    monkeypatch.setattr(sender, 'PICAMERA_CAMERA_INDEX', 'auto')
    cameras = (
        sender.CameraDescriptor(index=0, description='imx708 camera', modes=tuple()),
        sender.CameraDescriptor(index=1, description='imx219 camera', modes=tuple()),
    )

    selected = sender.resolve_selected_camera(cameras, 'imx219')
    assert selected is not None
    assert selected.index == 1


def test_resolve_requested_mode_uses_server_override():
    payload = {
        'video_profile': {
            'video_mode': 'h264,960,540,24',
        }
    }
    mode = sender.resolve_requested_mode(payload)
    assert mode is not None
    assert mode.width == 960
    assert mode.height == 540
    assert mode.fps == 24.0
