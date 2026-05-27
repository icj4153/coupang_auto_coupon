#!/bin/sh
set -eu

APP_DIR=${APP_DIR:-/volume1/docker/coupang_coupon}
KEY_DIR=${KEY_DIR:-/volume1/docker/coupang_coupon_secrets}
DOCKER=${DOCKER:-/usr/local/bin/docker}
COMPOSE=${COMPOSE:-/usr/local/bin/docker-compose}
PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

cd "$APP_DIR"

"$DOCKER" run --rm \
  -v "$APP_DIR:/repo" \
  -v "$KEY_DIR:/root/.ssh" \
  alpine/git \
  -C /repo \
  -c safe.directory=/repo \
  -c core.sshCommand="ssh -i /root/.ssh/github_deploy_key -o StrictHostKeyChecking=no" \
  pull --ff-only

mkdir -p data/logs data/browser_artifacts
if [ ! -f data/browser_coupon_config.json ]; then
  cp browser_coupon_config.server.example.json data/browser_coupon_config.json
fi

"$COMPOSE" up -d --build
