#!/bin/zsh
set -euo pipefail

NAS_HOST="${COUPON_NAS_HOST:-icj7297.synology.me}"
NAS_PORT="${COUPON_NAS_PORT:-2022}"
NAS_USER="${COUPON_NAS_USER:-joon_admin}"
NAS_KEY="${COUPON_NAS_KEY:-$HOME/.ssh/coupang_coupon_nas_actions}"
NAS_PROJECT_DIR="${COUPON_NAS_PROJECT_DIR:-/volume1/docker/coupang_coupon}"
REMOTE_NOVNC_PORT="${COUPON_REMOTE_NOVNC_PORT:-6080}"
NAS_LAN_HOST="${COUPON_NAS_LAN_HOST:-192.168.50.101}"
NOVNC_URL="${COUPON_NOVNC_URL:-http://${NAS_LAN_HOST}:${REMOTE_NOVNC_PORT}/vnc.html?autoconnect=true&resize=scale&host=${NAS_LAN_HOST}&port=${REMOTE_NOVNC_PORT}}"

SSH_BASE=(
  ssh
  -i "$NAS_KEY"
  -p "$NAS_PORT"
  -o BatchMode=yes
  "$NAS_USER@$NAS_HOST"
)

echo "[session-refresh] Starting NAS VNC login container..."
"${SSH_BASE[@]}" "export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:\$PATH; cd '$NAS_PROJECT_DIR' && /usr/local/bin/docker-compose -f docker-compose.yml -f docker-compose.login.yml up -d --build coupang-coupon-login-vnc"

echo "[session-refresh] Opening noVNC from the NAS LAN address."
echo "[session-refresh] VNC password is COUPON_VNC_PASSWORD, or COUPON_WEB_PASSWORD if COUPON_VNC_PASSWORD is not set."
echo "[session-refresh] noVNC URL: ${NOVNC_URL}"

open "$NOVNC_URL"

echo "[session-refresh] Log in inside the opened browser window."
echo "[session-refresh] This terminal can be closed after noVNC opens."
