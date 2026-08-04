# RC-Car-Arduino-UNO-Q

Remote RC car control stack with per-car session isolation, MediaMTX passthrough streaming, and board-safe command handling.

## What changed
- UNO Q Linux publishes H.264 once to MediaMTX over RTSP.
- Browser playback uses WebRTC WHEP from MediaMTX.
- Board commands are delivered over a persistent WebSocket.
- Command queues and stream state are scoped per car.
- Control actions require an authenticated session for the target car.

## Architecture
- Flask app:
   - Handles auth, booking, billing, per-session authorization
   - Issues per-car stream config to board and browser
   - Delivers control actions per-car to board over WebSocket
- MediaMTX:
   - Receives published H.264 stream for each car path
   - Relays to browser via WHEP/WebRTC
- Board Linux helpers:
   - `uno_q_board_stream_sender.py` publishes camera to MediaMTX RTSP target from server config
   - `uno_q_board_commands.py` consumes per-car command WebSocket and writes serial/RPC commands to MCU
- Raspberry Pi 4B + Camera Module 3 helpers:
   - `rpi4b_picam3_board_stream_sender.py` publishes Pi Camera H.264 via `libcamera-vid` to MediaMTX RTSP
   - `rpi4b_board_commands.py` consumes per-car command WebSocket and drives GPIO motor/light pins
- Arduino firmware:
   - Stops drivetrain if no motion command arrives for ~550 ms

## Quick start

1. Install Python dependencies:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Install and start MediaMTX on the server:
```bash
./install_mediamtx_linux.sh
sudo systemctl start mediamtx
```

3. Run the web app:
```bash
python app.py
```

4. Configure server env so app can emit stream URLs:
```bash
export MEDIAMTX_RTSP_BASE=rtsp://YOUR_SERVER_IP:8554
export MEDIAMTX_WHEP_BASE=https://YOUR_PUBLIC_DOMAIN
export MEDIAMTX_STREAM_PREFIX=cars
```

Port `8889` is intentionally bound to loopback in the production installer. Apache/Plesk
publishes MediaMTX under the same-origin `/cars/` path. Allow inbound TCP and UDP `8189`
through the provider and host firewalls for WebRTC media.

5. On board Linux side, run helpers:
```bash
# UNO Q board helpers
python3 uno_q_board_stream_sender.py
python3 uno_q_board_commands.py

# Raspberry Pi 4B board helpers
python3 rpi4b_picam3_board_stream_sender.py
python3 rpi4b_board_commands.py
```

## Board-side variables
- `SERVER_URL`: Flask server base URL
- `BOARD_TOKEN`: shared board token
- `BOARD_NAME`: unique car identity
- `VIDEO_DEVICE`: camera index or `/dev/videoN` or `auto`
- `VIDEO_MODE_AUTO`: select the closest supported V4L2 mode when the requested mode is unavailable (default `1`)
- `VIDEO_WIDTH`, `VIDEO_HEIGHT`, `FRAME_RATE`: preferred capture mode (defaults `1280x720@25`)
- `H264_ENCODER`: defaults to `libx264`; set an available encoder explicitly if the board supports low-latency hardware encode reliably
- `COMMAND_WS_URL`: optional explicit command WS URL
- `MEDIAMTX_RTSP_URL`: optional fixed override target (if not using server-provided URL)
- `BOARD_TYPE`: installer selector, use `uno_q` or `rpi4b` in `install_unified.conf` / `install_unified.local.conf`

Raspberry Pi 4B + Pi Camera 3 variables:
- `PICAMERA_BITRATE`: H.264 bitrate for `libcamera-vid` (default `3500000`)
- `GPIO_DRY_RUN`: if `1`, logs commands without toggling GPIO
- `DRIVE_IN1_PIN`, `DRIVE_IN2_PIN`: DRV8871 drivetrain PWM pins (defaults `18`, `19`)
- `FORWARD_THROTTLE`, `BACK_THROTTLE`: duty cycle from `0.0` to `1.0` for forward/reverse speed
- `SERVO_PIN`: PWM pin used for steering servo signal (default `12`)
- `SERVO_LEFT_ANGLE`, `SERVO_CENTER_ANGLE`, `SERVO_RIGHT_ANGLE`: steering command target angles
- `SERVO_MIN_ANGLE`, `SERVO_MAX_ANGLE`: servo output clamp limits
- `SERVO_MIN_PULSE_WIDTH`, `SERVO_MAX_PULSE_WIDTH`, `SERVO_FRAME_WIDTH`: servo pulse timing calibration
- `LIGHTS_PIN`: lights control pin (default `24`)
- `GPIO_ACTIVE_HIGH`: set `0` for active-low relay/driver boards

Raspberry Pi PWM note:
- For true PWM throttle with gpiozero, use PWM-capable GPIO pins (`12`, `13`, `18`, `19`).
- If a non-PWM pin is configured, the runtime falls back to digital on/off throttle for that channel.

## Server-side variables
- `BOARD_TOKEN`: shared board token
- `MEDIAMTX_RTSP_BASE`: RTSP publish base, e.g. `rtsp://server:8554`
- `MEDIAMTX_WHEP_BASE`: public same-origin base, e.g. `https://drive.kbob.org`
- `MEDIAMTX_STREAM_PREFIX`: path prefix, defaults to `cars`

Per-car path format:
- RTSP publish: `rtsp://server:8554/cars/<board_name>`
- Browser WHEP: `https://public-domain/cars/<board_name>/whep`

Board API and command WebSocket requests authenticate with `Authorization: Bearer <BOARD_TOKEN>`.
Tokens are not placed in query strings.

## Install scripts

Unified installer (runs git pull + install):
```bash
# server
MODE=server ./install_unified.sh

# board
MODE=board ./install_unified.sh
```

Installer defaults are in the tracked `install_unified.conf`. Put machine-specific overrides
in the gitignored `install_unified.local.conf` so `git pull` remains clean.
- Set repo/domain paths once in `install_unified.local.conf`.
- `BOARD_TOKEN` is auto-discovered from existing `.env` files when possible.
- If auto-discovery fails, copy the server `.env` token into `install_unified.local.conf`.
- The server installer preserves existing secrets and securely generates missing ones.

Server app install:
```bash
./install_server_linux.sh
```

Board install:
```bash
./install_uno_q_board_linux.sh

# Raspberry Pi 4B board install
./install_rpi4b_board_linux.sh
```

Unified board install by type:
```bash
# UNO Q board
BOARD_TYPE=uno_q MODE=board ./install_unified.sh

# Raspberry Pi 4B + Camera Module 3 board
BOARD_TYPE=rpi4b MODE=board ./install_unified.sh
```

MediaMTX config template is included in `mediamtx.yml`.
