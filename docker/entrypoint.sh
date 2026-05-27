#!/bin/sh
set -eu

mkdir -p /data/logs /data/browser_artifacts

if [ ! -f /data/browser_coupon_config.json ]; then
  cp /app/browser_coupon_config.server.example.json /data/browser_coupon_config.json
fi

exec "$@"
