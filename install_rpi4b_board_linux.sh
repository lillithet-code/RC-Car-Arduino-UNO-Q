#!/usr/bin/env bash
set -euo pipefail

BOARD_DIR="${BOARD_DIR:-$HOME/rc-car-rpi4b-board}"
VENV_DIR="${VENV_DIR:-$BOARD_DIR/.venv}"
SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

if [[ -f "$SCRIPT_DIR/uno_q_board_install_helpers.sh" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/uno_q_board_install_helpers.sh"
fi

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

discover_board_token() {
  if select_board_token; then
    return 0
  fi
  return 1
}

SERVER_URL="$(resolve_config_value SERVER_URL https://drive.kbob.org)"
SERVER_URL="${SERVER_URL%/}"
BOARD_TOKEN="$(resolve_config_value BOARD_TOKEN '')"
BOARD_NAME="$(resolve_config_value BOARD_NAME "$(hostname)")"
BOARD_LOCATION="$(resolve_config_value BOARD_LOCATION unknown)"
COMMAND_WS_URL="$(resolve_config_value COMMAND_WS_URL '')"
COMMAND_WS_RETRY_SECONDS="$(resolve_config_value COMMAND_WS_RETRY_SECONDS 1.0)"
STREAM_STATUS_POLL_SECONDS="$(resolve_config_value STREAM_STATUS_POLL_SECONDS 1.0)"
STREAM_DISABLE_GRACE_SECONDS="$(resolve_config_value STREAM_DISABLE_GRACE_SECONDS 8.0)"
MOTOR_WATCHDOG_MS="$(resolve_config_value MOTOR_WATCHDOG_MS 500)"
VIDEO_WIDTH="$(resolve_config_value VIDEO_WIDTH 1280)"
VIDEO_HEIGHT="$(resolve_config_value VIDEO_HEIGHT 720)"
PICAMERA_BITRATE="$(resolve_config_value PICAMERA_BITRATE 2000000)"
PICAMERA_PROFILE="$(resolve_config_value PICAMERA_PROFILE baseline)"
PICAMERA_LEVEL="$(resolve_config_value PICAMERA_LEVEL '')"
PICAMERA_SENSOR="$(resolve_config_value PICAMERA_SENSOR auto)"
PICAMERA_CAMERA_INDEX="$(resolve_config_value PICAMERA_CAMERA_INDEX auto)"
PICAMERA_IMX219_FRAME_RATE="$(resolve_config_value PICAMERA_IMX219_FRAME_RATE 30)"
PICAMERA_IMX219_VIDEO_WIDTH="$(resolve_config_value PICAMERA_IMX219_VIDEO_WIDTH 1280)"
PICAMERA_IMX219_VIDEO_HEIGHT="$(resolve_config_value PICAMERA_IMX219_VIDEO_HEIGHT 720)"
resolve_frame_rate_default() {
  local sensor_hint="${1:-auto}"
  case "$sensor_hint" in
    imx219)
      printf '30'
      ;;
    *)
      printf '60'
      ;;
  esac
}
FRAME_RATE="$(resolve_config_value FRAME_RATE "$(resolve_frame_rate_default "$PICAMERA_SENSOR")")"
MEDIAMTX_RTSP_URL="$(resolve_config_value MEDIAMTX_RTSP_URL '')"
GPIO_DRY_RUN="$(resolve_config_value GPIO_DRY_RUN 0)"
DRIVE_IN1_PIN="$(resolve_config_value DRIVE_IN1_PIN 18)"
DRIVE_IN2_PIN="$(resolve_config_value DRIVE_IN2_PIN 19)"
FORWARD_THROTTLE="$(resolve_config_value FORWARD_THROTTLE 0.65)"
BACK_THROTTLE="$(resolve_config_value BACK_THROTTLE 0.5)"
SERVO_PIN="$(resolve_config_value SERVO_PIN 12)"
SERVO_LEFT_ANGLE="$(resolve_config_value SERVO_LEFT_ANGLE -30)"
SERVO_CENTER_ANGLE="$(resolve_config_value SERVO_CENTER_ANGLE 0)"
SERVO_RIGHT_ANGLE="$(resolve_config_value SERVO_RIGHT_ANGLE 30)"
SERVO_MIN_ANGLE="$(resolve_config_value SERVO_MIN_ANGLE -90)"
SERVO_MAX_ANGLE="$(resolve_config_value SERVO_MAX_ANGLE 90)"
SERVO_MIN_PULSE_WIDTH="$(resolve_config_value SERVO_MIN_PULSE_WIDTH 0.0005)"
SERVO_MAX_PULSE_WIDTH="$(resolve_config_value SERVO_MAX_PULSE_WIDTH 0.0025)"
SERVO_FRAME_WIDTH="$(resolve_config_value SERVO_FRAME_WIDTH 0.02)"
LIGHTS_PIN="$(resolve_config_value LIGHTS_PIN 24)"
GPIO_ACTIVE_HIGH="$(resolve_config_value GPIO_ACTIVE_HIGH 1)"
GPIOZERO_PIN_FACTORY="$(resolve_config_value GPIOZERO_PIN_FACTORY lgpio)"
BOARD_RUN_USER="${BOARD_RUN_USER:-}"

if [[ -z "$BOARD_RUN_USER" && -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
  BOARD_RUN_USER="$SUDO_USER"
fi
BOARD_RUN_USER="${BOARD_RUN_USER:-$(id -un)}"
if ! id "$BOARD_RUN_USER" >/dev/null 2>&1; then
  echo "Error: BOARD_RUN_USER does not exist: $BOARD_RUN_USER" >&2
  exit 1
fi

if [[ -z "$BOARD_TOKEN" || "$BOARD_TOKEN" == "dev-board-token" ]]; then
  if ! discover_board_token; then
    echo "Error: BOARD_TOKEN must be set to the same non-default token used by the server." >&2
    exit 1
  fi
fi

mkdir -p "$BOARD_DIR"

sync_runtime_files() {
  local source_dir="${SCRIPT_DIR:-$PWD}"
  local target_dir="$BOARD_DIR"

  mkdir -p "$target_dir"
  for relative_path in rpi_csi_stream_sender.py rpi4b_picam3_board_stream_sender.py rpi4b_board_commands.py requirements_rpi4b_board.txt; do
    local source_file="$source_dir/$relative_path"
    local target_file="$target_dir/$relative_path"
    if [[ -f "$source_file" ]]; then
      if [[ "$source_file" != "$target_file" ]]; then
        cp "$source_file" "$target_file"
      fi
    fi
  done
}

sync_runtime_files
cd "$BOARD_DIR"

select_camera_package() {
  local package_name
  for package_name in rpicam-apps-lite rpicam-apps libcamera-apps; do
    local candidate
    candidate="$(apt-cache policy "$package_name" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
    if [[ -n "$candidate" && "$candidate" != "(none)" ]]; then
      printf '%s' "$package_name"
      return 0
    fi
  done
  return 1
}

if command -v apt-get >/dev/null 2>&1; then
  CAMERA_PACKAGE=""
  sudo apt-get update
  if CAMERA_PACKAGE="$(select_camera_package)"; then
    echo "Installing Pi camera package: $CAMERA_PACKAGE"
  else
    echo "Error: no supported Pi camera package is available (tried rpicam-apps-lite, rpicam-apps, libcamera-apps)." >&2
    exit 1
  fi
  sudo apt-get install -y python3 python3-pip python3-venv ffmpeg python3-gpiozero "$CAMERA_PACKAGE"

  PIGPIO_CANDIDATE="$(apt-cache policy pigpio 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
  PYTHON3_PIGPIO_CANDIDATE="$(apt-cache policy python3-pigpio 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"

  if [[ -n "$PIGPIO_CANDIDATE" && "$PIGPIO_CANDIDATE" != "(none)" ]]; then
    sudo apt-get install -y pigpio
  else
    echo "pigpio package not available on this distro; continuing with gpiozero default pin factory"
  fi

  if [[ -n "$PYTHON3_PIGPIO_CANDIDATE" && "$PYTHON3_PIGPIO_CANDIDATE" != "(none)" ]]; then
    sudo apt-get install -y python3-pigpio
  else
    echo "python3-pigpio package not available on this distro; continuing without pigpio Python bindings"
  fi

  PYTHON3_LGPIO_CANDIDATE="$(apt-cache policy python3-lgpio 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
  if [[ -n "$PYTHON3_LGPIO_CANDIDATE" && "$PYTHON3_LGPIO_CANDIDATE" != "(none)" ]]; then
    sudo apt-get install -y python3-lgpio
  else
    echo "python3-lgpio package not available on this distro; gpiozero will use fallback pin factories"
  fi
fi

if ! command -v rpicam-vid >/dev/null 2>&1 && ! command -v libcamera-vid >/dev/null 2>&1; then
  echo "Error: Pi camera publisher binary not found after install. Expected rpicam-vid or libcamera-vid on PATH." >&2
  exit 1
fi

if command -v getent >/dev/null 2>&1 && getent group video >/dev/null 2>&1; then
  if ! id -nG "$BOARD_RUN_USER" | tr ' ' '\n' | grep -qx 'video'; then
    echo "Adding $BOARD_RUN_USER to video group for camera device access"
    sudo usermod -aG video "$BOARD_RUN_USER"
  fi
fi

if command -v getent >/dev/null 2>&1 && getent group gpio >/dev/null 2>&1; then
  if ! id -nG "$BOARD_RUN_USER" | tr ' ' '\n' | grep -qx 'gpio'; then
    echo "Adding $BOARD_RUN_USER to gpio group for motor control pins"
    sudo usermod -aG gpio "$BOARD_RUN_USER"
  fi
fi

python3 -m venv --clear --system-site-packages "$VENV_DIR"
"$VENV_DIR/bin/python3" -m pip install --upgrade pip
"$VENV_DIR/bin/python3" -m pip install -r "$BOARD_DIR/requirements_rpi4b_board.txt"

if [[ "$GPIOZERO_PIN_FACTORY" == "lgpio" ]]; then
  if ! "$VENV_DIR/bin/python3" -c 'import lgpio' >/dev/null 2>&1; then
    echo "Warning: GPIOZERO_PIN_FACTORY=lgpio but the Python lgpio module is unavailable in $VENV_DIR." >&2
    echo "Falling back to GPIOZERO_PIN_FACTORY=native for this install." >&2
    GPIOZERO_PIN_FACTORY=native
  fi
fi

cat > "$BOARD_DIR/.env" <<EOF
SERVER_URL=$SERVER_URL
BOARD_TOKEN=$BOARD_TOKEN
BOARD_NAME=$BOARD_NAME
BOARD_LOCATION=$BOARD_LOCATION
COMMAND_WS_URL=$COMMAND_WS_URL
COMMAND_WS_RETRY_SECONDS=$COMMAND_WS_RETRY_SECONDS
STREAM_STATUS_POLL_SECONDS=$STREAM_STATUS_POLL_SECONDS
STREAM_DISABLE_GRACE_SECONDS=$STREAM_DISABLE_GRACE_SECONDS
MOTOR_WATCHDOG_MS=$MOTOR_WATCHDOG_MS
FRAME_RATE=$FRAME_RATE
VIDEO_WIDTH=$VIDEO_WIDTH
VIDEO_HEIGHT=$VIDEO_HEIGHT
PICAMERA_BITRATE=$PICAMERA_BITRATE
PICAMERA_PROFILE=$PICAMERA_PROFILE
PICAMERA_LEVEL=$PICAMERA_LEVEL
PICAMERA_SENSOR=$PICAMERA_SENSOR
PICAMERA_CAMERA_INDEX=$PICAMERA_CAMERA_INDEX
PICAMERA_IMX219_FRAME_RATE=$PICAMERA_IMX219_FRAME_RATE
PICAMERA_IMX219_VIDEO_WIDTH=$PICAMERA_IMX219_VIDEO_WIDTH
PICAMERA_IMX219_VIDEO_HEIGHT=$PICAMERA_IMX219_VIDEO_HEIGHT
MEDIAMTX_RTSP_URL=$MEDIAMTX_RTSP_URL
GPIO_DRY_RUN=$GPIO_DRY_RUN
DRIVE_IN1_PIN=$DRIVE_IN1_PIN
DRIVE_IN2_PIN=$DRIVE_IN2_PIN
FORWARD_THROTTLE=$FORWARD_THROTTLE
BACK_THROTTLE=$BACK_THROTTLE
SERVO_PIN=$SERVO_PIN
SERVO_LEFT_ANGLE=$SERVO_LEFT_ANGLE
SERVO_CENTER_ANGLE=$SERVO_CENTER_ANGLE
SERVO_RIGHT_ANGLE=$SERVO_RIGHT_ANGLE
SERVO_MIN_ANGLE=$SERVO_MIN_ANGLE
SERVO_MAX_ANGLE=$SERVO_MAX_ANGLE
SERVO_MIN_PULSE_WIDTH=$SERVO_MIN_PULSE_WIDTH
SERVO_MAX_PULSE_WIDTH=$SERVO_MAX_PULSE_WIDTH
SERVO_FRAME_WIDTH=$SERVO_FRAME_WIDTH
LIGHTS_PIN=$LIGHTS_PIN
GPIO_ACTIVE_HIGH=$GPIO_ACTIVE_HIGH
GPIOZERO_PIN_FACTORY=$GPIOZERO_PIN_FACTORY
EOF
sudo chown "$BOARD_RUN_USER":"$BOARD_RUN_USER" "$BOARD_DIR/.env"
sudo chmod 600 "$BOARD_DIR/.env"

cat > "$BOARD_DIR/start_rc_car_command.sh" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$DIR/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$DIR/.venv/bin/activate"
fi
set -a
if [[ -f "$DIR/.env" ]]; then
  # shellcheck disable=SC1091
  source "$DIR/.env"
fi
set +a
export PYTHONUNBUFFERED=1
exec python3 -u "$DIR/rpi4b_board_commands.py"
EOF2

chmod +x "$BOARD_DIR/start_rc_car_command.sh"

cat > "$BOARD_DIR/start_rc_car_stream.sh" <<'EOF3'
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$DIR/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$DIR/.venv/bin/activate"
fi
set -a
if [[ -f "$DIR/.env" ]]; then
  # shellcheck disable=SC1091
  source "$DIR/.env"
fi
set +a
export PYTHONUNBUFFERED=1
exec python3 -u "$DIR/rpi_csi_stream_sender.py"
EOF3

chmod +x "$BOARD_DIR/start_rc_car_stream.sh"

cat > "$BOARD_DIR/rc-car-command.service" <<EOF4
[Unit]
Description=RC Car Raspberry Pi command service
After=network.target

[Service]
WorkingDirectory=$BOARD_DIR
EnvironmentFile=$BOARD_DIR/.env
ExecStart=$BOARD_DIR/start_rc_car_command.sh
Restart=always
RestartSec=2
User=$BOARD_RUN_USER
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF4

cat > "$BOARD_DIR/rc-car-stream.service" <<EOF5
[Unit]
Description=RC Car Raspberry Pi stream service
After=network.target

[Service]
WorkingDirectory=$BOARD_DIR
EnvironmentFile=$BOARD_DIR/.env
ExecStart=$BOARD_DIR/start_rc_car_stream.sh
Restart=always
RestartSec=2
User=$BOARD_RUN_USER
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF5

sudo mv "$BOARD_DIR/rc-car-command.service" /etc/systemd/system/rc-car-command.service
sudo mv "$BOARD_DIR/rc-car-stream.service" /etc/systemd/system/rc-car-stream.service
sudo systemctl daemon-reload
sudo systemctl disable --now rc-car-rpi4b-board.service >/dev/null 2>&1 || true
sudo systemctl enable rc-car-command.service rc-car-stream.service
if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl restart rc-car-command.service rc-car-stream.service >/dev/null 2>&1 || true
fi

if command -v curl >/dev/null 2>&1; then
  BOARD_NAME_ENCODED="$(python3 - "$BOARD_NAME" <<'PY'
import sys
from urllib.parse import quote

print(quote(sys.argv[1], safe=''))
PY
)"
  STATUS_URL="$SERVER_URL/api/board/stream/status?board_name=$BOARD_NAME_ENCODED"
  TOKEN_CHECK_BODY="$(mktemp)"
  TOKEN_CHECK_CODE="$(curl -sS -o "$TOKEN_CHECK_BODY" -w '%{http_code}' -H "Authorization: Bearer $BOARD_TOKEN" --max-time 5 "$STATUS_URL" || true)"

  if [[ "$TOKEN_CHECK_CODE" == "403" ]]; then
    echo ""
    echo "Warning: board auth check returned HTTP 403 (token mismatch likely)."
    echo "Run this on the server to sync BOARD_TOKEN with this board token:"
    echo "  sudo sed -i 's|^BOARD_TOKEN=.*|BOARD_TOKEN=$BOARD_TOKEN|' ${SERVER_APP_DIR:-/var/www/vhosts/drive.kbob.org/httpdocs/rc-car-arduino-uno-q}/.env"
    echo "  sudo systemctl restart rc-car-web"
  elif [[ "$TOKEN_CHECK_CODE" =~ ^2 ]]; then
    echo "Board auth check passed: $STATUS_URL"
  else
    echo "Board auth check returned HTTP $TOKEN_CHECK_CODE; response body follows:"
    cat "$TOKEN_CHECK_BODY" || true
  fi

  rm -f "$TOKEN_CHECK_BODY"
fi

cat <<EOF
Raspberry Pi 4B board install completed.

Next steps:
1. Start services: sudo systemctl start rc-car-command rc-car-stream
2. Check command logs: sudo journalctl -u rc-car-command -f
3. Check stream logs: sudo journalctl -u rc-car-stream -f
3. If groups were changed, log out/in once so new video/gpio membership applies.
EOF
