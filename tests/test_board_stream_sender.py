from types import SimpleNamespace

import board_stream_sender as sender


V4L2_SAMPLE = """
ioctl: VIDIOC_ENUM_FMT
    Type: Video Capture

    [0]: 'MJPG' (Motion-JPEG, compressed)
        Size: Discrete 960x720
            Interval: Discrete 0.067s (15.000 fps)
        Size: Discrete 800x600
            Interval: Discrete 0.033s (30.000 fps)
        Size: Discrete 640x480
            Interval: Discrete 0.033s (30.000 fps)
    [1]: 'YUYV' (YUYV 4:2:2)
        Size: Discrete 640x480
            Interval: Discrete 0.067s (15.000 fps)
"""


def test_parse_v4l2_modes_normalizes_ffmpeg_input_formats():
    modes = sender.parse_v4l2_modes(V4L2_SAMPLE)
    assert sender.CameraMode('mjpeg', 800, 600, 30.0) in modes
    assert sender.CameraMode('yuyv422', 640, 480, 15.0) in modes


def test_camera_mode_selection_prefers_smooth_video_for_driving():
    requested = sender.CameraMode('mjpeg', 1920, 1080, 30.0)
    selected = sender.choose_camera_mode(sender.parse_v4l2_modes(V4L2_SAMPLE), requested)
    assert selected == sender.CameraMode('mjpeg', 800, 600, 30.0)


def test_parse_v4l2_modes_uses_interval_fps_for_stepwise_ranges():
    sample = """
ioctl: VIDIOC_ENUM_FMT
    Type: Video Capture

    [0]: 'MJPG' (Motion-JPEG, compressed)
        Size: Stepwise 1280x720 - 1920x1080 with step 1/1
            Interval: Discrete 0.067s (15.000 fps)
"""
    modes = sender.parse_v4l2_modes(sample)
    assert sender.CameraMode('mjpeg', 1920, 1080, 15.0) in modes


def test_resolve_h264_encoder_prefers_hw_encoder_when_available():
    selected = sender.resolve_h264_encoder(['h264_v4l2m2m', 'libx264'])
    assert selected == 'h264_v4l2m2m'


def test_resolve_h264_encoder_prefers_common_hw_encoder_when_available():
    selected = sender.resolve_h264_encoder(['h264_vaapi', 'libx264'])
    assert selected == 'h264_vaapi'


def test_resolve_h264_encoder_uses_project_priority_over_ffmpeg_listing_order():
    selected = sender.resolve_h264_encoder([
        'h264_nvenc',
        'h264_v4l2m2m',
        'h264_vaapi',
        'libx264',
    ])
    assert selected == 'h264_v4l2m2m'


def test_detect_available_ffmpeg_encoders_parses_ffmpeg_flag_column(monkeypatch):
    output = """
Encoders:
 V....D h264_v4l2m2m V4L2 mem2mem H.264 encoder wrapper
 V....D libx264         libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
"""
    monkeypatch.setattr(
        sender.subprocess,
        'run',
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output),
    )

    assert sender.detect_available_ffmpeg_encoders() == ['h264_v4l2m2m', 'libx264']


def test_camera_mode_selection_handles_stepwise_v4l2_ranges():
    sample = """
ioctl: VIDIOC_ENUM_FMT
    Type: Video Capture

    [0]: 'MJPG' (Motion-JPEG, compressed)
        Size: Stepwise 128x128 - 1920x1920 with step 1/1
            Interval: Discrete 0.033s (30.000 fps)
"""
    requested = sender.CameraMode('mjpeg', 1920, 1080, 30.0)
    selected = sender.choose_camera_mode(sender.parse_v4l2_modes(sample), requested)
    assert selected.width == 1920
    assert selected.height == 1080
    assert selected.fps == 30.0


def test_requested_legacy_resolution_is_upgraded_to_1080p30():
    requested = sender.resolve_requested_camera_mode({
        'VIDEO_WIDTH': '800',
        'VIDEO_HEIGHT': '600',
        'FRAME_RATE': '30',
        'H264_INPUT_FORMAT': 'mjpeg',
    })
    assert requested.width == 1920
    assert requested.height == 1080
    assert requested.fps == 30.0


def test_requested_low_fps_is_upgraded_to_30fps():
    requested = sender.resolve_requested_camera_mode({
        'VIDEO_WIDTH': '1920',
        'VIDEO_HEIGHT': '1080',
        'FRAME_RATE': '1',
        'H264_INPUT_FORMAT': 'mjpeg',
    })
    assert requested.width == 1920
    assert requested.height == 1080
    assert requested.fps == 30.0


def test_ffmpeg_command_forces_browser_compatible_h264(monkeypatch):
    monkeypatch.setattr(sender, 'resolve_h264_encoder', lambda: 'libx264')
    mode = sender.CameraMode('mjpeg', 1920, 1080, 30.0)
    command = sender.build_publish_command(
        0,
        'rtsp://example.test:8554/cars/RCCar1',
        mode,
    )
    assert command[command.index('-pix_fmt') + 1] == 'yuv420p'
    assert command[command.index('-profile:v') + 1] == 'baseline'
    assert command[command.index('-bf') + 1] == '0'
    assert command[command.index('-video_size') + 1] == '1920x1080'
    assert command[command.index('-framerate') + 1] == '30'


def test_ffmpeg_command_uses_nv12_for_v4l2_h264_encoder(monkeypatch):
    monkeypatch.setattr(sender, 'resolve_h264_encoder', lambda: 'h264_v4l2m2m')
    mode = sender.CameraMode('mjpeg', 1920, 1080, 30.0)

    command = sender.build_publish_command(
        0,
        'rtsp://example.test:8554/cars/RCCar1',
        mode,
    )

    assert command[command.index('-pix_fmt') + 1] == 'nv12'
