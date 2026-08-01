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
STREAM_STALE_SECONDS="${STREAM_STALE_SECONDS:-6.0}"
STREAM_LOSS_GRACE_SECONDS="${STREAM_LOSS_GRACE_SECONDS:-30.0}"
STREAM_CROP_BOTTOM_PX="${STREAM_CROP_BOTTOM_PX:-2}"
STREAM_JPEG_QUALITY="${STREAM_JPEG_QUALITY:-65}"
H264_INGEST_QUEUE_MAX="${H264_INGEST_QUEUE_MAX:-8}"
MEDIAMTX_RTSP_BASE="${MEDIAMTX_RTSP_BASE:-}"
MEDIAMTX_WHEP_BASE="${MEDIAMTX_WHEP_BASE:-}"
MEDIAMTX_STREAM_PREFIX="${MEDIAMTX_STREAM_PREFIX:-cars}"
if [[ -n "$PLESK_DOMAIN" ]]; then
  HOST="${HOST:-127.0.0.1}"
else
  HOST="${HOST:-0.0.0.0}"
fi
PORT="${PORT:-8000}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-1}"
GUNICORN_THREADS="${GUNICORN_THREADS:-8}"
RUN_USER="${RUN_USER:-$(id -un)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${SOURCE_DIR:-$SCRIPT_DIR}"
mkdir -p "$APP_DIR"

if [[ -n "$PLESK_DOMAIN" ]]; then
  if [[ -z "$MEDIAMTX_RTSP_BASE" ]]; then
    MEDIAMTX_RTSP_BASE="rtsp://$PLESK_DOMAIN:8554"
  fi
  if [[ -z "$MEDIAMTX_WHEP_BASE" ]]; then
    MEDIAMTX_WHEP_BASE="https://$PLESK_DOMAIN"
  fi
fi

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
STREAM_STALE_SECONDS=$STREAM_STALE_SECONDS
STREAM_LOSS_GRACE_SECONDS=$STREAM_LOSS_GRACE_SECONDS
STREAM_CROP_BOTTOM_PX=$STREAM_CROP_BOTTOM_PX
STREAM_JPEG_QUALITY=$STREAM_JPEG_QUALITY
H264_INGEST_QUEUE_MAX=$H264_INGEST_QUEUE_MAX
MEDIAMTX_RTSP_BASE=$MEDIAMTX_RTSP_BASE
MEDIAMTX_WHEP_BASE=$MEDIAMTX_WHEP_BASE
MEDIAMTX_STREAM_PREFIX=$MEDIAMTX_STREAM_PREFIX
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

  backup_if_exists() {
    local target="$1"
    if sudo test -f "$target"; then
      local stamp
      stamp="$(date +%Y%m%d%H%M%S)"
      sudo cp "$target" "${target}.bak.${stamp}"
      echo "Backed up $target to ${target}.bak.${stamp}"
    fi
  }

  upsert_marked_block() {
    local target="$1"
    local start_marker="$2"
    local end_marker="$3"
    local block_content="$4"
    local tmp_file
    tmp_file="$(mktemp)"

    if sudo test -f "$target"; then
      sudo awk -v start="$start_marker" -v end="$end_marker" '
        $0 ~ start { skipping=1; next }
        $0 ~ end { skipping=0; next }
        !skipping { print }
      ' "$target" > "$tmp_file"
    fi

    {
      if [[ -s "$tmp_file" ]]; then
        cat "$tmp_file"
      fi
      echo "$start_marker"
      printf '%b' "$block_content"
      echo "$end_marker"
    } | sudo tee "$target" >/dev/null

    rm -f "$tmp_file"
  }

  detect_plesk_frontend() {
    local apache_active=0
    local nginx_active=0
    if command -v systemctl >/dev/null 2>&1; then
      if systemctl is-active --quiet apache2 || systemctl is-active --quiet httpd; then
        apache_active=1
      fi
      if systemctl is-active --quiet nginx || systemctl is-active --quiet sw-nginx; then
        nginx_active=1
      fi
    fi

    if [[ "$apache_active" -eq 1 && "$nginx_active" -eq 1 ]]; then
      echo "hybrid"
    elif [[ "$apache_active" -eq 1 ]]; then
      echo "apache"
    elif [[ "$nginx_active" -eq 1 ]]; then
      echo "nginx"
    else
      echo "unknown"
    fi
  }

  if [[ -n "$PLESK_VHOST_DIR" ]]; then
    sudo mkdir -p "$PLESK_VHOST_DIR"
    FRONTEND_MODE="$(detect_plesk_frontend)"
    echo "Detected Plesk frontend mode: $FRONTEND_MODE"

    APACHE_PROXY_SNIPPET=''
    APACHE_PROXY_SNIPPET+='<IfModule mod_proxy.c>\n'
    APACHE_PROXY_SNIPPET+='    ProxyPreserveHost On\n'
    APACHE_PROXY_SNIPPET+='    ProxyPass "/cars/" "http://127.0.0.1:8889/cars/" retry=0 timeout=120 keepalive=On\n'
    APACHE_PROXY_SNIPPET+='    ProxyPassReverse "/cars/" "http://127.0.0.1:8889/cars/"\n'
    APACHE_PROXY_SNIPPET+='    ProxyPass "/ws/" "ws://127.0.0.1:'"$PORT"'/ws/" retry=0 timeout=120 keepalive=On\n'
    APACHE_PROXY_SNIPPET+='    ProxyPassReverse "/ws/" "ws://127.0.0.1:'"$PORT"'/ws/"\n'
    APACHE_PROXY_SNIPPET+='    ProxyPass "/" "http://127.0.0.1:'"$PORT"'/" retry=0 timeout=120 keepalive=On\n'
    APACHE_PROXY_SNIPPET+='    ProxyPassReverse "/" "http://127.0.0.1:'"$PORT"'/"\n'
    APACHE_PROXY_SNIPPET+='</IfModule>\n'

    APACHE_PROXY_SNIPPET+='<IfModule mod_headers.c>\n'
    APACHE_PROXY_SNIPPET+='    RequestHeader set X-Forwarded-Proto "https" env=HTTPS\n'
    APACHE_PROXY_SNIPPET+='</IfModule>\n'

    APACHE_HTTP_CONF="$PLESK_VHOST_DIR/vhost.conf"
    APACHE_HTTPS_CONF="$PLESK_VHOST_DIR/vhost_ssl.conf"
    APACHE_MARKER_START='# BEGIN RC-CAR CARS PROXY'
    APACHE_MARKER_END='# END RC-CAR CARS PROXY'

    backup_if_exists "$APACHE_HTTP_CONF"
    backup_if_exists "$APACHE_HTTPS_CONF"

    upsert_marked_block "$APACHE_HTTP_CONF" "$APACHE_MARKER_START" "$APACHE_MARKER_END" "$APACHE_PROXY_SNIPPET"
    upsert_marked_block "$APACHE_HTTPS_CONF" "$APACHE_MARKER_START" "$APACHE_MARKER_END" "$APACHE_PROXY_SNIPPET"
    echo "Created Plesk Apache reverse-proxy snippets at:"
    echo "  $APACHE_HTTP_CONF"
    echo "  $APACHE_HTTPS_CONF"

    if [[ "$FRONTEND_MODE" == "hybrid" || "$FRONTEND_MODE" == "nginx" ]]; then
      NGINX_CONF="$PLESK_VHOST_DIR/vhost_nginx.conf"
      NGINX_MARKER_START='# BEGIN RC-CAR CARS PROXY'
      NGINX_MARKER_END='# END RC-CAR CARS PROXY'
      backup_if_exists "$NGINX_CONF"
      NGINX_PROXY_SNIPPET="$(cat <<EOF_PLESK_NGINX
location /cars/ {
    proxy_pass http://127.0.0.1:8889/cars/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 86400;
    proxy_send_timeout 86400;
}

location / {
    proxy_pass http://127.0.0.1:$PORT/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 86400;
    proxy_send_timeout 86400;
}

location /ws/ {
  proxy_pass http://127.0.0.1:$PORT/ws/;
  proxy_http_version 1.1;
  proxy_set_header Upgrade \$http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host \$host;
  proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto \$scheme;
  proxy_read_timeout 86400;
  proxy_send_timeout 86400;
}
EOF_PLESK_NGINX
)"
      upsert_marked_block "$NGINX_CONF" "$NGINX_MARKER_START" "$NGINX_MARKER_END" "$NGINX_PROXY_SNIPPET"
      echo "Updated optional nginx snippet at $NGINX_CONF"
    fi

    if command -v a2enmod >/dev/null 2>&1; then
      sudo a2enmod proxy proxy_http proxy_wstunnel headers >/dev/null || true
    fi

    if command -v apache2ctl >/dev/null 2>&1; then
      echo "Running apache2ctl configtest before reload"
      sudo apache2ctl configtest
    elif command -v httpd >/dev/null 2>&1; then
      echo "Running httpd configtest before reload"
      sudo httpd -t
    fi

    if command -v plesk >/dev/null 2>&1; then
      sudo plesk sbin httpdmng --reconfigure-domain "$PLESK_DOMAIN"
    fi

    if command -v apache2ctl >/dev/null 2>&1; then
      echo "Running apache2ctl configtest after Plesk reconfigure"
      sudo apache2ctl configtest
    elif command -v httpd >/dev/null 2>&1; then
      echo "Running httpd configtest after Plesk reconfigure"
      sudo httpd -t
    fi

    if command -v systemctl >/dev/null 2>&1; then
      if systemctl is-active --quiet apache2; then
        sudo systemctl reload apache2
      elif systemctl is-active --quiet httpd; then
        sudo systemctl reload httpd
      fi
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
ExecStart=$APP_DIR/.venv/bin/gunicorn --worker-class gthread --workers $GUNICORN_WORKERS --threads $GUNICORN_THREADS --bind $HOST:$PORT app:app
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
