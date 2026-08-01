#!/usr/bin/env bash
set -euo pipefail

derive_board_token() {
  local repo_dir="${1:-$SCRIPT_DIR}"
  local token_source=""
  local candidate

  if [[ -n "$repo_dir" ]]; then
    candidate="$repo_dir/.env"
    if [[ -f "$candidate" ]]; then
      token_source="$(awk -F'=' '/^BOARD_TOKEN=/{print substr($0, index($0,$2)); exit}' "$candidate" 2>/dev/null || true)"
      if [[ -n "$token_source" ]]; then
        printf '%s' "$token_source"
        return 0
      fi
    fi
  fi

  if [[ -n "${BOARD_TOKEN:-}" && "$BOARD_TOKEN" != "dev-board-token" ]]; then
    printf '%s' "$BOARD_TOKEN"
    return 0
  fi

  if [[ -n "${SERVER_ENV_FILE:-}" && -f "$SERVER_ENV_FILE" ]]; then
    token_source="$(awk -F'=' '/^BOARD_TOKEN=/{print substr($0, index($0,$2)); exit}' "$SERVER_ENV_FILE" 2>/dev/null || true)"
    if [[ -n "$token_source" ]]; then
      printf '%s' "$token_source"
      return 0
    fi
  fi

  return 1
}

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

  if derived_token="$(derive_board_token "$SCRIPT_DIR")"; then
    BOARD_TOKEN="$derived_token"
    export BOARD_TOKEN
    return 0
  fi

  return 1
}
