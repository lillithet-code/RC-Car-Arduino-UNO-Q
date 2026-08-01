#!/usr/bin/env bash
set -euo pipefail

MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-1.15.2}"
INSTALL_DIR="${INSTALL_DIR:-/opt/mediamtx}"
MEDIAMTX_USER="${MEDIAMTX_USER:-mediamtx}"
MEDIAMTX_GROUP="${MEDIAMTX_GROUP:-mediamtx}"
HTTP_LISTEN="${MEDIAMTX_HTTP_LISTEN:-127.0.0.1:8889}"
RTSP_LISTEN="${MEDIAMTX_RTSP_LISTEN:-:8554}"
WEBRTC_ALLOW_ORIGIN="${MEDIAMTX_WEBRTC_ALLOW_ORIGIN:-*}"
WEBRTC_UDP_LISTEN="${MEDIAMTX_WEBRTC_UDP_LISTEN:-:8189}"
WEBRTC_TCP_LISTEN="${MEDIAMTX_WEBRTC_TCP_LISTEN:-:8189}"
WEBRTC_ADDITIONAL_HOST="${MEDIAMTX_WEBRTC_ADDITIONAL_HOST:-drive.kbob.org}"

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) ASSET_ARCH="amd64" ;;
  aarch64|arm64) ASSET_ARCH="arm64" ;;
  *)
    echo "Unsupported architecture: $ARCH" >&2
    exit 1
    ;;
esac

ASSET="mediamtx_v${MEDIAMTX_VERSION}_linux_${ASSET_ARCH}.tar.gz"
DOWNLOAD_URL="https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/${ASSET}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

if ! id -u "$MEDIAMTX_USER" >/dev/null 2>&1; then
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$MEDIAMTX_USER"
fi

sudo mkdir -p "$INSTALL_DIR"
sudo chown "$MEDIAMTX_USER":"$MEDIAMTX_GROUP" "$INSTALL_DIR"

curl -fsSL "$DOWNLOAD_URL" -o "$TMP_DIR/$ASSET"
tar -xzf "$TMP_DIR/$ASSET" -C "$TMP_DIR"

sudo install -m 0755 "$TMP_DIR/mediamtx" "$INSTALL_DIR/mediamtx"

sudo tee "$INSTALL_DIR/mediamtx.yml" >/dev/null <<EOF
logLevel: info
rtspAddress: ${RTSP_LISTEN}
webrtc: yes
webrtcAddress: ${HTTP_LISTEN}
webrtcAllowOrigin: '${WEBRTC_ALLOW_ORIGIN}'
webrtcLocalUDPAddress: ${WEBRTC_UDP_LISTEN}
webrtcLocalTCPAddress: ${WEBRTC_TCP_LISTEN}
webrtcIPsFromInterfaces: yes
webrtcAdditionalHosts:
  - ${WEBRTC_ADDITIONAL_HOST}
api: yes
apiAddress: 127.0.0.1:9997
EOF

sudo tee /etc/systemd/system/mediamtx.service >/dev/null <<EOF
[Unit]
Description=MediaMTX streaming relay
After=network.target

[Service]
Type=simple
User=${MEDIAMTX_USER}
Group=${MEDIAMTX_GROUP}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/mediamtx ${INSTALL_DIR}/mediamtx.yml
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mediamtx.service

echo "MediaMTX installed. Start with: sudo systemctl start mediamtx"
echo "Check logs with: sudo journalctl -u mediamtx -f"
