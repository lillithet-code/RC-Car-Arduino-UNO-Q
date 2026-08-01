#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-${MODE:-}}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_REMOTE="${REPO_REMOTE:-origin}"
SKIP_GIT_PULL="${SKIP_GIT_PULL:-0}"
CONFIG_FILE="${CONFIG_FILE:-install_unified.local.conf}"

if [[ -z "$MODE" ]]; then
  echo "Usage: $0 <server|board>" >&2
  echo "Or set MODE=server|board" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

load_config() {
  local defaults_path="$SCRIPT_DIR/install_unified.conf"
  local conf_path="$SCRIPT_DIR/$CONFIG_FILE"
  if [[ -f "$defaults_path" ]]; then
    # shellcheck disable=SC1090
    source "$defaults_path"
  fi
  if [[ "$conf_path" != "$defaults_path" && -f "$conf_path" ]]; then
    # shellcheck disable=SC1090
    source "$conf_path"
  fi
}

extract_token_from_env_file() {
  local env_file="$1"
  if [[ ! -f "$env_file" ]]; then
    return 1
  fi
  awk -F'=' '/^BOARD_TOKEN=/{print substr($0, index($0,$2)); exit}' "$env_file"
}

discover_board_token() {
  if [[ -n "${BOARD_TOKEN:-}" ]]; then
    return 0
  fi

  local candidate
  local candidates=(
    "${SERVER_APP_DIR:-}/.env"
    "${BOARD_DIR:-}/.env"
    "$SCRIPT_DIR/.env"
    "$HOME/.env"
  )

  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" ]] || continue
    if token_value="$(extract_token_from_env_file "$candidate" 2>/dev/null)" && [[ -n "$token_value" ]]; then
      BOARD_TOKEN="$token_value"
      export BOARD_TOKEN
      echo "Discovered BOARD_TOKEN from $candidate"
      return 0
    fi
  done

  return 1
}

export_install_defaults() {
  export PLESK_DOMAIN="${PLESK_DOMAIN:-drive.kbob.org}"
  export MEDIAMTX_WHEP_BASE="${MEDIAMTX_WHEP_BASE:-https://drive.kbob.org}"
  export MEDIAMTX_RTSP_BASE="${MEDIAMTX_RTSP_BASE:-rtsp://drive.kbob.org:8554}"
  export MEDIAMTX_WEBRTC_ADDITIONAL_HOST="${MEDIAMTX_WEBRTC_ADDITIONAL_HOST:-drive.kbob.org}"
  export SERVER_URL="${SERVER_URL:-https://drive.kbob.org}"
  export SERVER_APP_DIR="${SERVER_APP_DIR:-/var/www/vhosts/drive.kbob.org/httpdocs/rc-car-arduino-uno-q}"
  export BOARD_DIR="${BOARD_DIR:-/home/arduino/rc-car-arduino-uno-q}"
  export LEGACY_BOARD_DIR="${LEGACY_BOARD_DIR:-/home/arduino/RC-Car-Arduino-UNO-Q}"
  export BOARD_NAME="${BOARD_NAME:-RCCar1}"
}

run_git_pull() {
  if [[ "$SKIP_GIT_PULL" == "1" ]]; then
    echo "SKIP_GIT_PULL=1 set; skipping git pull"
    return 0
  fi

  if [[ ! -d .git ]]; then
    echo "Error: this directory is not a git checkout: $SCRIPT_DIR" >&2
    echo "Clone the repository first, then re-run this installer." >&2
    exit 1
  fi

  echo "Updating repository from $REPO_REMOTE/$REPO_BRANCH"
  git fetch "$REPO_REMOTE" "$REPO_BRANCH"
  git checkout "$REPO_BRANCH"
  git pull "$REPO_REMOTE" "$REPO_BRANCH"
}

install_server() {
  echo "Running server install"
  discover_board_token || true
  ./install_server_linux.sh

  echo "Running MediaMTX install"
  ./install_mediamtx_linux.sh

  echo "Restarting services"
  sudo systemctl restart mediamtx rc-car-web

  echo "Server install complete"
  echo "Verify with:"
  echo "  curl -i https://drive.kbob.org/api/health"
  echo "  curl -i -X OPTIONS https://drive.kbob.org/cars/test/whep"
}

install_board() {
  echo "Running board install"
  if ! discover_board_token; then
    echo "Error: no BOARD_TOKEN was supplied or found in an existing .env file." >&2
    echo "Copy the BOARD_TOKEN from the server .env into install_unified.local.conf." >&2
    exit 1
  fi
  ./install_board_linux.sh

  echo "Restarting board service"
  sudo systemctl restart rc-car-board

  echo "Board install complete"
  echo "Verify with:"
  echo "  sudo systemctl --no-pager --full status rc-car-board"
  echo "  sudo journalctl -u rc-car-board -n 120 --no-pager"
}

load_config
export_install_defaults
run_git_pull

case "$MODE" in
  server)
    install_server
    ;;
  board)
    install_board
    ;;
  *)
    echo "Invalid mode: $MODE" >&2
    echo "Expected: server or board" >&2
    exit 1
    ;;
esac
