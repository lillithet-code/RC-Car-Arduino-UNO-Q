# RC-Car-Arduino-UNO-Q

Remote RC car control stack with per-car session isolation, MediaMTX passthrough streaming, and board-safe command handling.

## What changed
- H.264-over-HTTP chunk ingest is removed from the primary path.
- UNO Q Linux now publishes H.264 once to MediaMTX (RTSP).
- Browser playback uses WebRTC WHEP from MediaMTX (no server-side decode/re-encode).
- Board command delivery is now persistent WebSocket (no 20 Hz polling loop).
- Command queues and stream state are scoped per car.
- Control API now requires logged-in user ownership of an active session.
- MCU firmware includes a 550 ms motion-command failsafe timeout.

## Architecture
- Flask app:
   - Handles auth, booking, billing, per-session authorization
   - Issues per-car stream config to board and browser
   - Delivers control actions per-car to board over WebSocket
- MediaMTX:
   - Receives published H.264 stream for each car path
   - Relays to browser via WHEP/WebRTC
- Board Linux helpers:
   - `board_stream_sender.py` publishes camera to MediaMTX RTSP target from server config
   - `board_commands.py` consumes per-car command WebSocket and writes serial/RPC commands to MCU
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
python3 board_stream_sender.py
python3 board_commands.py
```

## Board-side variables
- `SERVER_URL`: Flask server base URL
- `BOARD_TOKEN`: shared board token
- `BOARD_NAME`: unique car identity
- `VIDEO_DEVICE`: camera index or `/dev/videoN` or `auto`
- `VIDEO_MODE_AUTO`: select the closest supported V4L2 mode when the requested mode is unavailable (default `1`)
- `VIDEO_WIDTH`, `VIDEO_HEIGHT`, `FRAME_RATE`: preferred capture mode (defaults `1920x1080@30`)
- `H264_ENCODER`: defaults to `libx264`; browser output is forced to H.264 baseline/yuv420p
- `COMMAND_WS_URL`: optional explicit command WS URL
- `MEDIAMTX_RTSP_URL`: optional fixed override target (if not using server-provided URL)

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
./install_board_linux.sh
```

MediaMTX config template is included in `mediamtx.yml`.
