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


def test_parse_camera_modes_supports_rpicam_format_lines():
    lines = [
        "    'SRGGB10_CSI2P' : 1536x864 [120.13 fps - (1536, 864)/4608x2592 crop]",
        "                       2304x1296 [56.03 fps - (2304, 1296)/4608x2592 crop]",
        "                       4608x2592 [14.35 fps - (0, 0)/4608x2592 crop]",
    ]

    modes = sender._parse_camera_modes(lines)
    assert len(modes) == 3
    assert modes[0] == sender.CameraMode(input_format='srggb10_csi2p', width=1536, height=864, fps=120.13)
    assert modes[1] == sender.CameraMode(input_format='srggb10_csi2p', width=2304, height=1296, fps=56.03)
    assert modes[2] == sender.CameraMode(input_format='srggb10_csi2p', width=4608, height=2592, fps=14.35)


def test_select_best_mode_keeps_requested_when_size_not_present():
    requested = sender.CameraMode(input_format='h264', width=1280, height=720, fps=30.0)
    camera = sender.CameraDescriptor(
        index=0,
        description='imx708 sample',
        modes=(
            sender.CameraMode(input_format='srggb10_csi2p', width=1536, height=864, fps=120.13),
            sender.CameraMode(input_format='srggb10_csi2p', width=2304, height=1296, fps=56.03),
        ),
    )

    selected = sender.select_best_mode(requested, camera)
    assert selected == requested


def test_build_reported_modes_includes_common_and_camera_modes():
    requested = sender.CameraMode(input_format='h264', width=1280, height=720, fps=30.0)
    camera = sender.CameraDescriptor(
        index=0,
        description='imx708 sample',
        modes=(
            sender.CameraMode(input_format='srggb10_csi2p', width=1536, height=864, fps=120.13),
            sender.CameraMode(input_format='srggb10_csi2p', width=2304, height=1296, fps=56.03),
        ),
    )

    modes = sender.build_reported_modes(camera, requested)
    values = {(mode.width, mode.height, round(mode.fps, 2), mode.input_format) for mode in modes}

    assert (1280, 720, 30.0, 'h264') in values
    assert (1920, 1080, 30.0, 'h264') in values
    assert (1536, 864, 120.13, 'h264') in values
    assert (2304, 1296, 56.03, 'h264') in values


def test_apply_cpu_governor_updates_all_cpu_paths(monkeypatch, tmp_path):
    cpu0 = tmp_path / 'cpu0_governor'
    cpu1 = tmp_path / 'cpu1_governor'
    cpu0.write_text('ondemand', encoding='utf-8')
    cpu1.write_text('ondemand', encoding='utf-8')

    monkeypatch.setattr(sender, 'CPU_GOVERNOR_CONTROL_ENABLED', True)
    monkeypatch.setattr(sender, 'glob', lambda pattern: [str(cpu0), str(cpu1)])

    applied = sender.apply_cpu_governor('powersave', None)
    assert applied == 'powersave'
    assert cpu0.read_text(encoding='utf-8') == 'powersave'
    assert cpu1.read_text(encoding='utf-8') == 'powersave'


def test_apply_cpu_governor_noops_when_disabled(monkeypatch, tmp_path):
    cpu0 = tmp_path / 'cpu0_governor'
    cpu0.write_text('ondemand', encoding='utf-8')

    monkeypatch.setattr(sender, 'CPU_GOVERNOR_CONTROL_ENABLED', False)
    monkeypatch.setattr(sender, 'glob', lambda pattern: [str(cpu0)])

    applied = sender.apply_cpu_governor('powersave', None)
    assert applied is None
    assert cpu0.read_text(encoding='utf-8') == 'ondemand'


def test_apply_platform_power_saving_runs_bluetooth_and_hdmi(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return True

    monkeypatch.setattr(sender, 'DISABLE_BLUETOOTH', True)
    monkeypatch.setattr(sender, 'DISABLE_HDMI', True)
    monkeypatch.setattr(sender, 'run_optional_command', fake_run)

    bluetooth_disabled, hdmi_disabled = sender.apply_platform_power_saving(False, False)

    assert bluetooth_disabled is True
    assert hdmi_disabled is True
    assert calls == [
        ['rfkill', 'block', 'bluetooth'],
        ['vcgencmd', 'display_power', '0'],
    ]


def test_apply_platform_power_saving_skips_completed_actions(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return True

    monkeypatch.setattr(sender, 'DISABLE_BLUETOOTH', True)
    monkeypatch.setattr(sender, 'DISABLE_HDMI', True)
    monkeypatch.setattr(sender, 'run_optional_command', fake_run)

    bluetooth_disabled, hdmi_disabled = sender.apply_platform_power_saving(True, True)

    assert bluetooth_disabled is True
    assert hdmi_disabled is True
    assert calls == []
