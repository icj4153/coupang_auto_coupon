#!/bin/sh
set -eu

mkdir -p /data/logs /data/browser_artifacts /tmp/.X11-unix

VNC_PASSWORD="${COUPON_VNC_PASSWORD:-${COUPON_WEB_PASSWORD:-}}"
if [ -z "$VNC_PASSWORD" ]; then
  echo "[vnc-login] COUPON_VNC_PASSWORD or COUPON_WEB_PASSWORD is required." >&2
  exit 1
fi

export DISPLAY="${DISPLAY:-:99}"
SCREEN="${COUPON_VNC_SCREEN:-1440x950x24}"

echo "[vnc-login] Starting Xvfb on ${DISPLAY}."
Xvfb "$DISPLAY" -screen 0 "$SCREEN" -ac +extension RANDR > /data/logs/vnc-login-xvfb.log 2>&1 &
sleep 1

echo "[vnc-login] Starting window manager."
fluxbox > /data/logs/vnc-login-fluxbox.log 2>&1 &

echo "[vnc-login] Starting VNC server."
x11vnc \
  -display "$DISPLAY" \
  -forever \
  -shared \
  -rfbport 5900 \
  -passwd "$VNC_PASSWORD" \
  -o /data/logs/vnc-login-x11vnc.log \
  > /dev/null 2>&1 &

echo "[vnc-login] Starting noVNC on port 6080."
websockify --web=/usr/share/novnc/ 6080 localhost:5900 > /data/logs/vnc-login-websockify.log 2>&1 &

echo "[vnc-login] Open noVNC, log in to Coupang WING, then wait for the session to be saved."
exec python3 /app/wing_coupon_browser.py \
  --config "${COUPON_CONFIG_PATH:-/data/browser_coupon_config.json}" \
  --setup-login \
  --fresh-login \
  --setup-login-timeout-minutes "${COUPON_SETUP_LOGIN_TIMEOUT_MINUTES:-30}"
