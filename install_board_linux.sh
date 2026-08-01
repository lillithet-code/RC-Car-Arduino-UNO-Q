#!/usr/bin/env bash
set -euo pipefail

BOARD_DIR="${BOARD_DIR:-$HOME/rc-car-arduino-uno-q}"
VENV_DIR="${VENV_DIR:-$BOARD_DIR/.venv}"

read_existing_env_value() {
  local key="$1"
  local env_file="$BOARD_DIR/.env"
  [[ -f "$env_file" ]] || return 1
  awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "$env_file"
}

resolve_config_value() {
  local key="$1"
  local default_value="$2"
  local supplied_value="${!key:-}"
  if [[ -n "$supplied_value" ]]; then
    printf '%s' "$supplied_value"
    return
  fi
  local existing_value
  existing_value="$(read_existing_env_value "$key" 2>/dev/null || true)"
  printf '%s' "${existing_value:-$default_value}"
}

SERVER_URL="$(resolve_config_value SERVER_URL https://drive.kbob.org)"
SERVER_URL="${SERVER_URL%/}"
BOARD_TOKEN="$(resolve_config_value BOARD_TOKEN '')"
BOARD_NAME="$(resolve_config_value BOARD_NAME "$(hostname)")"
BOARD_LOCATION="$(resolve_config_value BOARD_LOCATION unknown)"
SERIAL_PORT="$(resolve_config_value SERIAL_PORT /dev/ttyACM0)"
SERIAL_BAUD_RATE="$(resolve_config_value SERIAL_BAUD_RATE 9600)"
SERIAL_RETRY_SECONDS="$(resolve_config_value SERIAL_RETRY_SECONDS 3.0)"
SERIAL_EXCLUSIVE="$(resolve_config_value SERIAL_EXCLUSIVE 1)"
COMMAND_BRIDGE="$(resolve_config_value COMMAND_BRIDGE '')"
ROUTER_SOCKET_PATH="$(resolve_config_value ROUTER_SOCKET_PATH /var/run/arduino-router.sock)"
COMMAND_WS_URL="$(resolve_config_value COMMAND_WS_URL '')"
COMMAND_WS_RETRY_SECONDS="$(resolve_config_value COMMAND_WS_RETRY_SECONDS 1.0)"
FRAME_RATE="$(resolve_config_value FRAME_RATE 30)"
VIDEO_WIDTH="$(resolve_config_value VIDEO_WIDTH 800)"
VIDEO_HEIGHT="$(resolve_config_value VIDEO_HEIGHT 600)"
VIDEO_MODE_AUTO="$(resolve_config_value VIDEO_MODE_AUTO 1)"
VIDEO_DEVICE_AUTO_RECOVER="$(resolve_config_value VIDEO_DEVICE_AUTO_RECOVER 1)"
STREAM_STATUS_POLL_SECONDS="$(resolve_config_value STREAM_STATUS_POLL_SECONDS 1.0)"
STREAM_DISABLE_GRACE_SECONDS="$(resolve_config_value STREAM_DISABLE_GRACE_SECONDS 8.0)"
H264_ENCODER="$(resolve_config_value H264_ENCODER libx264)"
H264_PROFILE="$(resolve_config_value H264_PROFILE baseline)"
H264_BITRATE="$(resolve_config_value H264_BITRATE 2500k)"
H264_MAXRATE="$(resolve_config_value H264_MAXRATE 2500k)"
H264_BUFSIZE="$(resolve_config_value H264_BUFSIZE 1500k)"
H264_GOP="$(resolve_config_value H264_GOP 30)"
H264_INPUT_FORMAT="$(resolve_config_value H264_INPUT_FORMAT '')"
H264_INPUT_FORMATS="$(resolve_config_value H264_INPUT_FORMATS mjpeg)"
VIDEO_REPROBE_DELAY_SECONDS="$(resolve_config_value VIDEO_REPROBE_DELAY_SECONDS 1.0)"
VIDEO_DEVICE="$(resolve_config_value VIDEO_DEVICE auto)"
MEDIAMTX_RTSP_URL="$(resolve_config_value MEDIAMTX_RTSP_URL '')"
BOARD_RUN_USER="${BOARD_RUN_USER:-}"

if [[ -z "$BOARD_RUN_USER" ]]; then
  BOARD_PARENT="$(dirname "$BOARD_DIR")"
  if [[ -d "$BOARD_PARENT" ]]; then
    PARENT_OWNER="$(stat -c '%U' "$BOARD_PARENT" 2>/dev/null || true)"
    if [[ -n "$PARENT_OWNER" && "$PARENT_OWNER" != "root" ]]; then
      BOARD_RUN_USER="$PARENT_OWNER"
    fi
  fi
fi
if [[ -z "$BOARD_RUN_USER" && -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
  BOARD_RUN_USER="$SUDO_USER"
fi
BOARD_RUN_USER="${BOARD_RUN_USER:-$(id -un)}"
if ! id "$BOARD_RUN_USER" >/dev/null 2>&1; then
  echo "Error: BOARD_RUN_USER does not exist: $BOARD_RUN_USER" >&2
  exit 1
fi

if [[ -z "$BOARD_TOKEN" || "$BOARD_TOKEN" == "dev-board-token" ]]; then
  echo "Error: BOARD_TOKEN must be set to the same non-default token used by the server." >&2
  exit 1
fi

if [[ -z "$COMMAND_BRIDGE" && -S "$ROUTER_SOCKET_PATH" ]]; then
  COMMAND_BRIDGE="rpc://$ROUTER_SOCKET_PATH"
fi

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
  sudo apt-get install -y python3 python3-pip python3-venv python3-dev build-essential libgl1 libglib2.0-0 ffmpeg v4l-utils
fi

if command -v getent >/dev/null 2>&1 && getent group video >/dev/null 2>&1; then
  if ! id -nG "$BOARD_RUN_USER" | tr ' ' '\n' | grep -qx 'video'; then
    echo "Adding $BOARD_RUN_USER to video group for camera device access"
    sudo usermod -aG video "$BOARD_RUN_USER"
  fi
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$BOARD_DIR/requirements.txt"
"$VENV_DIR/bin/pip" install pyserial
"$VENV_DIR/bin/pip" install msgpack

if [[ "$VIDEO_DEVICE" =~ ^/dev/video([0-9]+)$ ]]; then
  VIDEO_DEVICE="${BASH_REMATCH[1]}"
fi

if [[ "$VIDEO_DEVICE" == "auto" ]]; then
  if DETECTED_VIDEO_DEVICE="$(autodetect_first_working_video_device)"; then
    echo "Auto mode enabled; current detected camera index: $DETECTED_VIDEO_DEVICE"
  else
    echo "Warning: unable to auto-detect a working video device right now. VIDEO_DEVICE=auto will keep retrying at runtime."
  fi
fi

if [[ -z "$H264_INPUT_FORMAT" ]]; then
  H264_INPUT_FORMAT="$H264_INPUT_FORMATS"
fi

cat > "$BOARD_DIR/.env" <<EOF
SERVER_URL=$SERVER_URL
BOARD_TOKEN=$BOARD_TOKEN
BOARD_NAME=$BOARD_NAME
BOARD_LOCATION=$BOARD_LOCATION
SERIAL_PORT=$SERIAL_PORT
SERIAL_BAUD_RATE=$SERIAL_BAUD_RATE
SERIAL_RETRY_SECONDS=$SERIAL_RETRY_SECONDS
SERIAL_EXCLUSIVE=$SERIAL_EXCLUSIVE
COMMAND_BRIDGE=$COMMAND_BRIDGE
ROUTER_SOCKET_PATH=$ROUTER_SOCKET_PATH
COMMAND_WS_URL=$COMMAND_WS_URL
COMMAND_WS_RETRY_SECONDS=$COMMAND_WS_RETRY_SECONDS
FRAME_RATE=$FRAME_RATE
VIDEO_WIDTH=$VIDEO_WIDTH
VIDEO_HEIGHT=$VIDEO_HEIGHT
VIDEO_MODE_AUTO=$VIDEO_MODE_AUTO
VIDEO_DEVICE_AUTO_RECOVER=$VIDEO_DEVICE_AUTO_RECOVER
STREAM_STATUS_POLL_SECONDS=$STREAM_STATUS_POLL_SECONDS
STREAM_DISABLE_GRACE_SECONDS=$STREAM_DISABLE_GRACE_SECONDS
H264_ENCODER=$H264_ENCODER
H264_PROFILE=$H264_PROFILE
H264_BITRATE=$H264_BITRATE
H264_MAXRATE=$H264_MAXRATE
H264_BUFSIZE=$H264_BUFSIZE
H264_GOP=$H264_GOP
H264_INPUT_FORMAT=$H264_INPUT_FORMAT
H264_INPUT_FORMATS=$H264_INPUT_FORMATS
VIDEO_REPROBE_DELAY_SECONDS=$VIDEO_REPROBE_DELAY_SECONDS
VIDEO_DEVICE=$VIDEO_DEVICE
MEDIAMTX_RTSP_URL=$MEDIAMTX_RTSP_URL
EOF
sudo chown "$BOARD_RUN_USER":"$BOARD_RUN_USER" "$BOARD_DIR/.env"
sudo chmod 600 "$BOARD_DIR/.env"

cat > "$BOARD_DIR/start_board.sh" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/.venv/bin/activate"
set -a
source "$DIR/.env"
set +a
export PYTHONUNBUFFERED=1
python3 -u "$DIR/board_stream_sender.py" &
STREAM_PID=$!
python3 -u "$DIR/board_commands.py" &
COMMAND_PID=$!

shutdown_children() {
  kill "$STREAM_PID" "$COMMAND_PID" 2>/dev/null || true
  wait "$STREAM_PID" "$COMMAND_PID" 2>/dev/null || true
}
trap shutdown_children EXIT INT TERM

set +e
wait -n "$STREAM_PID" "$COMMAND_PID"
CHILD_STATUS=$?
set -e
echo "A board child process exited (status=$CHILD_STATUS); stopping its sibling so systemd can restart both." >&2
if [[ "$CHILD_STATUS" -eq 0 ]]; then
  exit 1
fi
exit "$CHILD_STATUS"
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
User=$BOARD_RUN_USER

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
