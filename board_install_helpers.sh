#!/usr/bin/env bash
set -euo pipefail

select_board_token() {
  local candidate
  local token_value
  local candidates=(
    "$SCRIPT_DIR/.env"
    "$HOME/.env"
    "$PWD/.env"
  )

  if [[ -n "${SERVER_APP_DIR:-}" ]]; then
    candidates+=("$SERVER_APP_DIR/.env")
  fi
  if [[ -n "${SERVER_ENV_FILE:-}" ]]; then
    candidates+=("$SERVER_ENV_FILE")
  fi
  if [[ -n "${PLESK_WEB_ROOT:-}" ]]; then
    candidates+=("$PLESK_WEB_ROOT/rc-car-arduino-uno-q/.env")
  fi
  candidates+=("$BOARD_DIR/.env")

  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" ]] || continue
    if token_value="$(awk -F'=' '/^BOARD_TOKEN=/{print substr($0, index($0,$2)); exit}' "$candidate" 2>/dev/null)" && [[ -n "$token_value" ]]; then
      if [[ -n "${BOARD_TOKEN:-}" && "$BOARD_TOKEN" != "dev-board-token" ]]; then
        return 0
      fi
      BOARD_TOKEN="$token_value"
      export BOARD_TOKEN
      return 0
    fi
  done

  return 1
}
