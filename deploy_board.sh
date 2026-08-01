#!/usr/bin/env bash
set -euo pipefail

BOARD_HOST="${1:-user@board-host}"
BOARD_DIR="${BOARD_DIR:-/home/user/rc-car-arduino-uno-q}"
REMOTE_SCRIPT="$BOARD_DIR/install_board_linux.sh"
SERVER_URL="${SERVER_URL:-https://drive.kbob.org}"
BOARD_TOKEN="${BOARD_TOKEN:-}"

if [[ -z "${BOARD_HOST}" ]]; then
  echo "Usage: ./deploy_board.sh user@board-host" >&2
  exit 1
fi
if [[ -z "$BOARD_TOKEN" || "$BOARD_TOKEN" == "dev-board-token" ]]; then
  echo "Error: set BOARD_TOKEN to the same non-default token used by the server." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

extract_token_from_env_file() {
  local env_file="$1"
  if [[ ! -f "$env_file" ]]; then
    return 1
  fi
  awk -F'=' '/^BOARD_TOKEN=/{print substr($0, index($0,$2)); exit}' "$env_file" 2>/dev/null
}

discover_board_token() {
  local token_value
  local candidate

  if [[ -n "${BOARD_TOKEN:-}" ]]; then
    return 0
  fi

  local candidates=(
    "$SCRIPT_DIR/.env"
    "$HOME/.env"
    "$HOME/rc-car-arduino-uno-q/.env"
    "$BOARD_DIR/.env"
  )

  for candidate in "${candidates[@]}"; do
    if token_value="$(extract_token_from_env_file "$candidate" 2>/dev/null)" && [[ -n "$token_value" ]]; then
      BOARD_TOKEN="$token_value"
      export BOARD_TOKEN
      return 0
    fi
  done

  if ssh "$BOARD_HOST" "test -f '$BOARD_DIR/.env'" >/dev/null 2>&1; then
    if token_value="$(ssh "$BOARD_HOST" "awk -F= '/^BOARD_TOKEN=/{print substr(\$0, index(\$0,\$2)); exit}' '$BOARD_DIR/.env' 2>/dev/null")" && [[ -n "$token_value" ]]; then
      BOARD_TOKEN="$token_value"
      export BOARD_TOKEN
      return 0
    fi
  fi

  BOARD_TOKEN="${BOARD_TOKEN:-dev-board-token}"
  export BOARD_TOKEN
}

discover_board_token

ssh "$BOARD_HOST" "mkdir -p '$BOARD_DIR'"
scp -r "$SCRIPT_DIR/." "$BOARD_HOST:$BOARD_DIR/"
ssh "$BOARD_HOST" "SERVER_URL='$SERVER_URL' BOARD_TOKEN='$BOARD_TOKEN' bash $REMOTE_SCRIPT"

echo "Deployment complete."
echo "Run the following on the board if needed:"
echo "  sudo systemctl start rc-car-board"
