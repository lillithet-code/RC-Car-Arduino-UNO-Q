#!/usr/bin/env bash
set -euo pipefail

BOARD_NAME="${1:-${BOARD_NAME:-test}}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://drive.kbob.org}"
MEDIAMTX_API_BASE="${MEDIAMTX_API_BASE:-http://127.0.0.1:9997}"
PATH_PREFIX="${MEDIAMTX_STREAM_PREFIX:-cars}"
STREAM_PATH="${PATH_PREFIX}/${BOARD_NAME}"
WHEP_URL="${PUBLIC_BASE_URL%/}/${STREAM_PATH}/whep"

print_header() {
  echo
  echo "== $1 =="
}

service_state() {
  local unit="$1"
  if systemctl list-unit-files | grep -q "^${unit}"; then
    systemctl is-active "$unit" 2>/dev/null || true
  else
    echo "not-installed"
  fi
}

show_service_logs() {
  local unit="$1"
  if systemctl list-unit-files | grep -q "^${unit}"; then
    journalctl -u "$unit" -n 40 --no-pager || true
  fi
}

print_header "Service state"
echo "rc-car-web.service: $(service_state rc-car-web.service)"
echo "mediamtx.service: $(service_state mediamtx.service)"
if systemctl list-unit-files | grep -q '^rc-car-board\.service'; then
  echo "rc-car-board.service: $(service_state rc-car-board.service)"
else
  echo "rc-car-board.service: not-installed (likely server host)"
fi

print_header "Listening ports"
ss -luntp | grep -E '(:8000|:8554|:8889|:8189)\b' || true

print_header "MediaMTX control API"
API_URL="${MEDIAMTX_API_BASE%/}/v3/paths/get/${STREAM_PATH}"
API_BODY="$(curl -sS --max-time 2 "$API_URL" || true)"
API_CODE="$(curl -sS -o /tmp/rc-car-mediamtx-api.json -w '%{http_code}' --max-time 2 "$API_URL" || true)"
echo "GET $API_URL -> HTTP $API_CODE"
if [[ -s /tmp/rc-car-mediamtx-api.json ]]; then
  cat /tmp/rc-car-mediamtx-api.json
  python3 - <<'PY'
import json
from pathlib import Path
p = Path('/tmp/rc-car-mediamtx-api.json')
try:
    data = json.loads(p.read_text())
except Exception as exc:
    print(f'parse_error={exc}')
    raise SystemExit(0)

tracks = data.get('tracks')
if tracks is None and isinstance(data.get('source'), dict):
    tracks = data['source'].get('tracks')
if tracks is None:
    tracks = []
if isinstance(tracks, dict):
    tracks = list(tracks.values())

bytes_received = None
for key in ('bytesReceived', 'bytes_received', 'receivedBytes'):
    if key in data and data[key] is not None:
        bytes_received = data[key]
        break
if bytes_received is None and isinstance(data.get('source'), dict):
    for key in ('bytesReceived', 'bytes_received', 'receivedBytes'):
        if key in data['source'] and data['source'][key] is not None:
            bytes_received = data['source'][key]
            break

ready = bool(data.get('ready') or data.get('sourceReady'))
h264 = any('h264' in str(track).lower() for track in tracks)
print('summary:')
print(f'  ready={ready}')
print(f'  tracks={tracks}')
print(f'  has_h264={h264}')
print(f'  bytes_received={bytes_received}')
PY
else
  echo "no API body returned"
fi

print_header "Public WHEP routing"
echo "OPTIONS $WHEP_URL"
curl -sS -D - -o /tmp/rc-car-whep-options.out -X OPTIONS --max-time 4 "$WHEP_URL" || true

print_header "RTSP reachability (from this host)"
if command -v nc >/dev/null 2>&1; then
  nc -zvw2 127.0.0.1 8554 || true
else
  echo "nc not installed; skipping RTSP port probe"
fi

print_header "ICE transport ports"
if command -v nc >/dev/null 2>&1; then
  nc -zvw2 127.0.0.1 8189 || true
else
  echo "nc not installed; skipping TCP 8189 probe"
fi

echo "UDP 8189 local socket check:"
ss -lun | grep -E '(:8189)\b' || true

print_header "Firewall checks"
if command -v nft >/dev/null 2>&1; then
  echo "nft ruleset (first 120 lines):"
  nft list ruleset 2>/dev/null | sed -n '1,120p' || true
fi
if command -v iptables >/dev/null 2>&1; then
  echo "iptables -S (first 120 lines):"
  iptables -S 2>/dev/null | sed -n '1,120p' || true
fi

print_header "Recent service logs"
show_service_logs rc-car-web.service
show_service_logs mediamtx.service
show_service_logs rc-car-board.service

print_header "Camera devices and formats"
ls -l /dev/video* 2>/dev/null || echo "No /dev/video* devices found"
if command -v v4l2-ctl >/dev/null 2>&1; then
  for dev in /dev/video*; do
    [[ -e "$dev" ]] || continue
    echo "--- $dev ---"
    v4l2-ctl -d "$dev" --list-formats-ext || true
  done
else
  echo "v4l2-ctl not installed"
fi

echo
echo "Diagnosis complete for board: $BOARD_NAME"
