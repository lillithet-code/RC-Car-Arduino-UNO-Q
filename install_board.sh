#!/usr/bin/env bash
set -euo pipefail

# Compatibility entry point. The previous file accidentally installed the web
# server despite being named install_board.sh.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/install_board_linux.sh" "$@"
