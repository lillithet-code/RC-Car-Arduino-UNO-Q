#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
SECRET_KEY="${SECRET_KEY:-change-me}"
DATABASE_URL="${DATABASE_URL:-sqlite:///$APP_DIR/app.db}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
PLEX_INSTALL="${PLEX_INSTALL:-0}"
PLEX_MEDIA_SERVER_DIR="${PLEX_MEDIA_SERVER_DIR:-$APP_DIR/plex-media-server}"

mkdir -p "$APP_DIR"
cd "$APP_DIR"

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-pip python3-venv python3-dev build-essential libgl1 libglib2.0-0 curl
fi

if command -v docker >/dev/null 2>&1; then
  echo "Removing Docker to free space..."
  sudo systemctl stop docker 2>/dev/null || true
  sudo apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true
  sudo apt-get autoremove -y 2>/dev/null || true
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"
"$VENV_DIR/bin/pip" install gunicorn

if [[ "$PLEX_INSTALL" == "1" ]]; then
  echo "Preparing Plex server prerequisites..."
  sudo apt-get install -y ffmpeg apt-transport-https gnupg lsb-release
  mkdir -p "$PLEX_MEDIA_SERVER_DIR"
  cat > "$PLEX_MEDIA_SERVER_DIR/start_plex.sh" <<'EOF_PLEX'
#!/usr/bin/env bash
set -euo pipefail
printf 'Plex media server placeholder. Install the Plex server package manually if needed.\n'
EOF_PLEX
  chmod +x "$PLEX_MEDIA_SERVER_DIR/start_plex.sh"
fi

cat > "$APP_DIR/.env" <<EOF_ENV
SECRET_KEY=$SECRET_KEY
DATABASE_URL=$DATABASE_URL
HOST=$HOST
PORT=$PORT
EOF_ENV

cat > "$APP_DIR/rc-car-web.service" <<EOF_SERVICE
[Unit]
Description=RC Car web app
After=network.target

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/gunicorn --bind 0.0.0.0:$PORT app:app
Restart=always
RestartSec=5
User=$USER

[Install]
WantedBy=multi-user.target
EOF_SERVICE

sudo mv "$APP_DIR/rc-car-web.service" /etc/systemd/system/rc-car-web.service
sudo systemctl daemon-reload
sudo systemctl enable rc-car-web.service

cat <<EOF
Server install completed.

Next steps:
1. Review the environment file at $APP_DIR/.env
2. Start the service: sudo systemctl start rc-car-web
3. Check logs: sudo journalctl -u rc-car-web -f
EOF
