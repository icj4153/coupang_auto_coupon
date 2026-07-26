#!/bin/sh
set -eu

APP_DIR=${APP_DIR:-/volume1/docker/coupang_coupon}
KEY_DIR=${KEY_DIR:-/volume1/docker/coupang_coupon_secrets}
DOCKER=${DOCKER:-/usr/local/bin/docker}
COMPOSE=${COMPOSE:-/usr/local/bin/docker-compose}
GIT_IMAGE=${GIT_IMAGE:-alpine/git}
DEPLOY_BRANCH=${DEPLOY_BRANCH:-main}
PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

cd "$APP_DIR"
mkdir -p data/logs data/browser_artifacts

if [ -f "$KEY_DIR/github_deploy_key" ]; then
  git_cmd() {
    "$DOCKER" run --rm \
      -v "$APP_DIR:/repo" \
      -v "$KEY_DIR:/root/.ssh" \
      "$GIT_IMAGE" \
      -C /repo \
      -c safe.directory=/repo \
      -c core.sshCommand="ssh -i /root/.ssh/github_deploy_key -o StrictHostKeyChecking=no" \
      "$@"
  }
else
  git_cmd() {
    "$DOCKER" run --rm \
      -v "$APP_DIR:/repo" \
      "$GIT_IMAGE" \
      -C /repo \
      -c safe.directory=/repo \
      "$@"
  }
fi

stamp=$(date +%Y%m%d%H%M%S)
git_cmd status --short > "data/logs/pre_deploy_git_status_$stamp.txt" || true
if [ -s "data/logs/pre_deploy_git_status_$stamp.txt" ]; then
  git_cmd diff > "data/logs/pre_deploy_git_diff_$stamp.patch" || true
fi

git_cmd fetch --prune origin "$DEPLOY_BRANCH"
git_cmd reset --hard "origin/$DEPLOY_BRANCH"
git_cmd clean -fd

if [ ! -f data/browser_coupon_config.json ]; then
  cp browser_coupon_config.server.example.json data/browser_coupon_config.json
fi

"$COMPOSE" up -d --build
