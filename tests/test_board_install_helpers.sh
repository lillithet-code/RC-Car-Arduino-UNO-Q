#!/usr/bin/env bash
set -euo pipefail

TEST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER_PATH="$TEST_ROOT/board_install_helpers.sh"

if [[ ! -f "$HELPER_PATH" ]]; then
  echo "helper script not found: $HELPER_PATH" >&2
  exit 1
fi

source "$HELPER_PATH"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

BOARD_DIR="$TMP_DIR/board"
mkdir -p "$BOARD_DIR"
cat > "$BOARD_DIR/.env" <<'EOF'
BOARD_TOKEN=stale-board-token
EOF

SERVER_ENV_FILE="$TMP_DIR/server.env"
cat > "$SERVER_ENV_FILE" <<'EOF'
BOARD_TOKEN=server-shared-token
EOF

BOARD_TOKEN=""
SCRIPT_DIR="$TEST_ROOT"
BOARD_DIR="$BOARD_DIR"
SERVER_ENV_FILE="$SERVER_ENV_FILE"

select_board_token

if [[ "$BOARD_TOKEN" != "server-shared-token" ]]; then
  echo "expected server-shared-token but got: $BOARD_TOKEN" >&2
  exit 1
fi

echo "token selection passed"
