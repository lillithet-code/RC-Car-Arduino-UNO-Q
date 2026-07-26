#!/usr/bin/env bash
set -euo pipefail

BOARD_DIR="${BOARD_DIR:-$HOME/rc-car-arduino-uno-q}"
VENV_DIR="${VENV_DIR:-$BOARD_DIR/.venv}"
SERVER_URL="${SERVER_URL:-https://drive.kbob.org}"
SERVER_URL="${SERVER_URL%/}"
BOARD_TOKEN="${BOARD_TOKEN:-dev-board-token}"
SERIAL_PORT="${SERIAL_PORT:-/dev/ttyACM0}"
SERIAL_BAUD_RATE="${SERIAL_BAUD_RATE:-9600}"
SERIAL_RETRY_SECONDS="${SERIAL_RETRY_SECONDS:-1.0}"
SERIAL_EXCLUSIVE="${SERIAL_EXCLUSIVE:-1}"
FRAME_RATE="${FRAME_RATE:-6}"
JPEG_QUALITY="${JPEG_QUALITY:-70}"
UPLOAD_TIMEOUT_SECONDS="${UPLOAD_TIMEOUT_SECONDS:-5.0}"
VIDEO_DEVICE="${VIDEO_DEVICE:-0}"

probe_video_device() {
  local device_index="$1"
  "$VENV_DIR/bin/python3" - "$device_index" <<'PY'
import sys
import time

import cv2

index = int(sys.argv[1])
cap = cv2.VideoCapture(index)
if not cap.isOpened():
    raise SystemExit(1)

ok = False
for _ in range(12):
    ret, _ = cap.read()
    if ret:
        ok = True
        break
    time.sleep(0.05)

cap.release()
raise SystemExit(0 if ok else 1)
PY
}

autodetect_first_working_video_device() {
  local path
  local index

  shopt -s nullglob
  for path in /dev/video*; do
    if [[ "$path" =~ ^/dev/video([0-9]+)$ ]]; then
      index="${BASH_REMATCH[1]}"
      if probe_video_device "$index"; then
        echo "$index"
        return 0
      fi
    fi
  done

  return 1
}

mkdir -p "$BOARD_DIR"
cd "$BOARD_DIR"

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-pip python3-venv python3-dev build-essential libgl1 libglib2.0-0
fi

if command -v docker >/dev/null 2>&1; then
  echo "Removing Docker to free space..."
  sudo systemctl stop docker 2>/dev/null || true
  sudo apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true
  sudo apt-get autoremove -y 2>/dev/null || true
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$BOARD_DIR/requirements.txt"
"$VENV_DIR/bin/pip" install pyserial

if [[ "$VIDEO_DEVICE" =~ ^/dev/video([0-9]+)$ ]]; then
  VIDEO_DEVICE="${BASH_REMATCH[1]}"
fi

if [[ "$VIDEO_DEVICE" == "0" || "$VIDEO_DEVICE" == "auto" ]]; then
  if DETECTED_VIDEO_DEVICE="$(autodetect_first_working_video_device)"; then
    VIDEO_DEVICE="$DETECTED_VIDEO_DEVICE"
    echo "Auto-detected working video device index: $VIDEO_DEVICE"
  else
    echo "Warning: unable to auto-detect a working video device. Using VIDEO_DEVICE=$VIDEO_DEVICE"
  fi
fi

cat > "$BOARD_DIR/.env" <<EOF
SERVER_URL=$SERVER_URL
BOARD_TOKEN=$BOARD_TOKEN
SERIAL_PORT=$SERIAL_PORT
SERIAL_BAUD_RATE=$SERIAL_BAUD_RATE
SERIAL_RETRY_SECONDS=$SERIAL_RETRY_SECONDS
SERIAL_EXCLUSIVE=$SERIAL_EXCLUSIVE
FRAME_RATE=$FRAME_RATE
JPEG_QUALITY=$JPEG_QUALITY
UPLOAD_TIMEOUT_SECONDS=$UPLOAD_TIMEOUT_SECONDS
VIDEO_DEVICE=$VIDEO_DEVICE
EOF

cat > "$BOARD_DIR/start_board.sh" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/.venv/bin/activate"
set -a
source "$DIR/.env"
set +a
python3 "$DIR/board_stream_sender.py" &
python3 "$DIR/board_commands.py" &
wait
EOF2

chmod +x "$BOARD_DIR/start_board.sh"

cat > "$BOARD_DIR/rc-car-board.service" <<EOF3
[Unit]
Description=RC Car board stream and command service
After=network.target

[Service]
WorkingDirectory=$BOARD_DIR
EnvironmentFile=$BOARD_DIR/.env
ExecStart=$BOARD_DIR/start_board.sh
Restart=always
RestartSec=5
User=$USER

[Install]
WantedBy=multi-user.target
EOF3

sudo mv "$BOARD_DIR/rc-car-board.service" /etc/systemd/system/rc-car-board.service
sudo systemctl daemon-reload
sudo systemctl enable rc-car-board.service

cat <<EOF
Board/Linux install completed.

Next steps:
1. Start the service: sudo systemctl start rc-car-board
2. Check logs: sudo journalctl -u rc-car-board -f
EOF
