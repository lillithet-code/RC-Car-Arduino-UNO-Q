#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-${MODE:-}}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_REMOTE="${REPO_REMOTE:-origin}"
SKIP_GIT_PULL="${SKIP_GIT_PULL:-0}"

if [[ -z "$MODE" ]]; then
  echo "Usage: $0 <server|board>" >&2
  echo "Or set MODE=server|board" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

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
  ./install_board_linux.sh

  echo "Restarting board service"
  sudo systemctl restart rc-car-board

  echo "Board install complete"
  echo "Verify with:"
  echo "  sudo systemctl --no-pager --full status rc-car-board"
  echo "  sudo journalctl -u rc-car-board -n 120 --no-pager"
}

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
