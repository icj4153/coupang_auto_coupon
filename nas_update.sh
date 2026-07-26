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

LOCK_DIR="$APP_DIR/data/.deploy.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  now=$(date +%s)
  lock_started=$(cat "$LOCK_DIR/started_at" 2>/dev/null || echo 0)
  case "$lock_started" in
    ''|*[!0-9]*) lock_started=0 ;;
  esac
  lock_age=$((now - lock_started))
  if [ "$lock_age" -gt 3600 ]; then
    echo "Removing stale deploy lock older than 1 hour: $LOCK_DIR"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR"
  else
    echo "Another deploy is already running. Leaving current containers untouched."
    exit 0
  fi
fi
date +%s > "$LOCK_DIR/started_at"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

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

remove_service_containers() {
  for name in coupang-coupon-web coupang-coupon-scheduler; do
    ids=$("$DOCKER" ps -aq --filter "name=$name" 2>/dev/null || true)
    if [ -n "$ids" ]; then
      echo "Removing stale container(s) matching $name"
      "$DOCKER" rm -f $ids || true
    fi
  done
}

if ! "$COMPOSE" up -d --build --remove-orphans; then
  echo "docker-compose up failed. Cleaning service containers and retrying once."
  remove_service_containers
  "$COMPOSE" up -d --build --remove-orphans
fi
