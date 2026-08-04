#!/usr/bin/env bash
set -euo pipefail

TEST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER_PATH="$TEST_ROOT/uno_q_board_install_helpers.sh"

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

BOARD_TOKEN=""
BOARD_DIR="$TEST_ROOT"
SERVER_ENV_FILE=""
SCRIPT_DIR="$TEST_ROOT"
mkdir -p "$TEST_ROOT/.tmp_repo"
cat > "$TEST_ROOT/.tmp_repo/.env" <<'EOF'
BOARD_TOKEN=repo-checkout-token
EOF

derived_token="$(derive_board_token "$TEST_ROOT/.tmp_repo")"
if [[ -z "$derived_token" ]]; then
  echo "expected derived token from repository checkout" >&2
  exit 1
fi

if [[ "$derived_token" != "repo-checkout-token" ]]; then
  echo "expected repo-checkout-token from repository checkout" >&2
  exit 1
fi

echo "token selection passed"
