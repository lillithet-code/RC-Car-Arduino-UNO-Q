#!/usr/bin/env bash
set -euo pipefail

BOARD_HOST="${1:-user@board-host}"
BOARD_DIR="${BOARD_DIR:-/home/user/rc-car-arduino-uno-q}"
REMOTE_SCRIPT="$BOARD_DIR/install_board.sh"
SERVER_URL="${SERVER_URL:-https://drive.kbob.org}"
BOARD_TOKEN="${BOARD_TOKEN:-dev-board-token}"

if [[ -z "${BOARD_HOST}" ]]; then
  echo "Usage: ./deploy_board.sh user@board-host" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

ssh "$BOARD_HOST" "mkdir -p '$BOARD_DIR'"
scp -r "$SCRIPT_DIR" "$BOARD_HOST:$BOARD_DIR"
ssh "$BOARD_HOST" "SERVER_URL='$SERVER_URL' BOARD_TOKEN='$BOARD_TOKEN' bash $REMOTE_SCRIPT"

echo "Deployment complete."
echo "Run the following on the board if needed:"
echo "  sudo systemctl start rc-car-board"
