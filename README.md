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
export MEDIAMTX_WHEP_BASE=http://YOUR_SERVER_IP:8889
export MEDIAMTX_STREAM_PREFIX=cars
```

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
- `COMMAND_WS_URL`: optional explicit command WS URL
- `MEDIAMTX_RTSP_URL`: optional fixed override target (if not using server-provided URL)

## Server-side variables
- `BOARD_TOKEN`: shared board token
- `MEDIAMTX_RTSP_BASE`: RTSP publish base, e.g. `rtsp://server:8554`
- `MEDIAMTX_WHEP_BASE`: WebRTC/WHEP base, e.g. `http://server:8889`
- `MEDIAMTX_STREAM_PREFIX`: path prefix, defaults to `cars`

Per-car path format:
- RTSP publish: `rtsp://server:8554/cars/<board_name>`
- Browser WHEP: `http://server:8889/cars/<board_name>/whep`

## Install scripts

Server app install:
```bash
./install_server_linux.sh
```

Board install:
```bash
./install_board_linux.sh
```

MediaMTX config template is included in `mediamtx.yml`.

