#!/usr/bin/env bash
set -euo pipefail

PLESK_DOMAIN="${PLESK_DOMAIN:-drive.kbob.org}"
PLESK_SUBDOMAIN="${PLESK_SUBDOMAIN:-}"

if [[ -n "$PLESK_DOMAIN" ]]; then
  if [[ -n "$PLESK_SUBDOMAIN" ]]; then
    PLESK_WEB_ROOT="/var/www/vhosts/$PLESK_DOMAIN/subdomains/$PLESK_SUBDOMAIN/httpdocs"
  else
    PLESK_WEB_ROOT="/var/www/vhosts/$PLESK_DOMAIN/httpdocs"
  fi
  APP_DIR="${APP_DIR:-$PLESK_WEB_ROOT/rc-car-arduino-uno-q}"
else
  APP_DIR="${APP_DIR:-$HOME/rc-car-arduino-uno-q}"
fi

VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
SECRET_KEY="${SECRET_KEY:-change-me}"
BOARD_TOKEN="${BOARD_TOKEN:-dev-board-token}"
DATABASE_URL="${DATABASE_URL:-sqlite:///$APP_DIR/app.db}"
STREAM_PROFILE="${STREAM_PROFILE:-0}"
BOARD_POLL_URL="${BOARD_POLL_URL:-}"
BOARD_POLL_INTERVAL_SECONDS="${BOARD_POLL_INTERVAL_SECONDS:-2.0}"
BOARD_POLL_TIMEOUT_SECONDS="${BOARD_POLL_TIMEOUT_SECONDS:-1.5}"
BOARD_ACTIVE_STALE_SECONDS="${BOARD_ACTIVE_STALE_SECONDS:-8.0}"
if [[ -n "$PLESK_DOMAIN" ]]; then
  HOST="${HOST:-127.0.0.1}"
else
  HOST="${HOST:-0.0.0.0}"
fi
PORT="${PORT:-8000}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-3}"
GUNICORN_THREADS="${GUNICORN_THREADS:-4}"
RUN_USER="${RUN_USER:-$(id -un)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${SOURCE_DIR:-$SCRIPT_DIR}"
mkdir -p "$APP_DIR"

if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then
  echo "Syncing source files from $SOURCE_DIR to $APP_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a \
      --exclude '.git/' \
      --exclude '.venv/' \
      --exclude '__pycache__/' \
      --exclude '.env' \
      --exclude 'app.db' \
      "$SOURCE_DIR/" "$APP_DIR/"
  else
    cp -a "$SOURCE_DIR/." "$APP_DIR/"
    rm -rf "$APP_DIR/.git" "$APP_DIR/.venv" "$APP_DIR/__pycache__" 2>/dev/null || true
  fi
fi

if command -v sudo >/dev/null 2>&1; then
  sudo chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"
fi

if [[ ! -f "$APP_DIR/requirements.txt" ]]; then
  echo "Error: requirements.txt not found in APP_DIR=$APP_DIR" >&2
  echo "Set SOURCE_DIR to a valid project checkout before running installer." >&2
  exit 1
fi

cd "$APP_DIR"

echo "Installing application for domain: $PLESK_DOMAIN"
echo "Application path: $APP_DIR"

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-pip python3-venv python3-dev build-essential libgl1 libglib2.0-0 curl ffmpeg apt-transport-https gnupg lsb-release
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

cat > "$APP_DIR/.env" <<EOF_ENV
SECRET_KEY=$SECRET_KEY
BOARD_TOKEN=$BOARD_TOKEN
DATABASE_URL=$DATABASE_URL
STREAM_PROFILE=$STREAM_PROFILE
BOARD_POLL_URL=$BOARD_POLL_URL
BOARD_POLL_INTERVAL_SECONDS=$BOARD_POLL_INTERVAL_SECONDS
BOARD_POLL_TIMEOUT_SECONDS=$BOARD_POLL_TIMEOUT_SECONDS
BOARD_ACTIVE_STALE_SECONDS=$BOARD_ACTIVE_STALE_SECONDS
HOST=$HOST
PORT=$PORT
GUNICORN_WORKERS=$GUNICORN_WORKERS
GUNICORN_THREADS=$GUNICORN_THREADS
EOF_ENV

if [[ -n "$PLESK_DOMAIN" ]]; then
  PLESK_VHOST_DIR=""
  for candidate in \
    "/var/www/vhosts/system/$PLESK_DOMAIN/conf" \
    "/var/www/vhosts/$PLESK_DOMAIN/conf"
  do
    if [[ -d "$candidate" ]]; then
      PLESK_VHOST_DIR="$candidate"
      break
    fi
  done

  if [[ -n "$PLESK_VHOST_DIR" ]]; then
    sudo mkdir -p "$PLESK_VHOST_DIR"
    sudo tee "$PLESK_VHOST_DIR/vhost_nginx.conf" >/dev/null <<EOF_PLESK
location / {
    proxy_pass http://127.0.0.1:$PORT;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
}
EOF_PLESK
    echo "Created Plesk reverse-proxy config at $PLESK_VHOST_DIR/vhost_nginx.conf"

    if command -v plesk >/dev/null 2>&1; then
      sudo plesk sbin httpdmng --reconfigure-domain "$PLESK_DOMAIN" || true
    fi
  else
    echo "Plesk domain config directory not found; please add a reverse proxy manually for $PLESK_DOMAIN"
  fi
fi

cat > "$APP_DIR/rc-car-web.service" <<EOF_SERVICE
[Unit]
Description=RC Car web app
After=network.target

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/gunicorn --workers $GUNICORN_WORKERS --threads $GUNICORN_THREADS --bind $HOST:$PORT app:app
Restart=always
RestartSec=5
User=$RUN_USER

[Install]
WantedBy=multi-user.target
EOF_SERVICE

sudo mv "$APP_DIR/rc-car-web.service" /etc/systemd/system/rc-car-web.service
sudo systemctl daemon-reload
sudo systemctl enable rc-car-web.service

cat <<EOF
Server/Linux install completed.

Next steps:
1. Start the service: sudo systemctl start rc-car-web
2. Check logs: sudo journalctl -u rc-car-web -f
3. If you are using Plesk, make sure the domain or subdomain is pointed at the Plesk vhost and the generated reverse-proxy snippet is enabled for that vhost.
4. If you provided PLESK_DOMAIN, the app will be installed under $APP_DIR and will listen on $HOST:$PORT for the Plesk proxy.
EOF
