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


def test_ffmpeg_command_forces_browser_compatible_h264():
    mode = sender.CameraMode('mjpeg', 1920, 1080, 30.0)
    command = sender.build_publish_command(0, 'rtsp://example.test:8554/cars/RCCar1', mode)
    assert command[command.index('-pix_fmt') + 1] == 'yuv420p'
    assert command[command.index('-profile:v') + 1] == 'baseline'
    assert command[command.index('-bf') + 1] == '0'
    assert command[command.index('-video_size') + 1] == '1920x1080'
    assert command[command.index('-framerate') + 1] == '30'
