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
DATABASE_URL="${DATABASE_URL:-sqlite:///$APP_DIR/app.db}"
if [[ -n "$PLESK_DOMAIN" ]]; then
  HOST="${HOST:-127.0.0.1}"
else
  HOST="${HOST:-0.0.0.0}"
fi
PORT="${PORT:-8000}"
mkdir -p "$APP_DIR"
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
DATABASE_URL=$DATABASE_URL
HOST=$HOST
PORT=$PORT
EOF_ENV

if [[ -n "$PLESK_DOMAIN" ]]; then
  PLESK_VHOST_DIR="/var/www/vhosts/$PLESK_DOMAIN/conf"
  if [[ -d "$PLESK_VHOST_DIR" ]]; then
    sudo mkdir -p "$PLESK_VHOST_DIR"
    sudo tee "$PLESK_VHOST_DIR/rc-car-proxy.conf" >/dev/null <<EOF_PLESK
ProxyPreserveHost On
ProxyPass / http://127.0.0.1:$PORT/
ProxyPassReverse / http://127.0.0.1:$PORT/
EOF_PLESK
    echo "Created Plesk reverse-proxy config at $PLESK_VHOST_DIR/rc-car-proxy.conf"
  else
    echo "Plesk domain directory not found at $PLESK_VHOST_DIR; please add a reverse proxy manually for $PLESK_DOMAIN"
  fi
fi

cat > "$APP_DIR/rc-car-web.service" <<EOF_SERVICE
[Unit]
Description=RC Car web app
After=network.target

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/gunicorn --bind $HOST:$PORT app:app
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
Server/Linux install completed.

Next steps:
1. Start the service: sudo systemctl start rc-car-web
2. Check logs: sudo journalctl -u rc-car-web -f
3. If you are using Plesk, make sure the domain or subdomain is pointed at the Plesk vhost and the generated reverse-proxy snippet is enabled for that vhost.
4. If you provided PLESK_DOMAIN, the app will be installed under $APP_DIR and will listen on $HOST:$PORT for the Plesk proxy.
EOF
