#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

NAS_HOST="${COUPON_NAS_HOST:-icj7297.synology.me}"
NAS_PORT="${COUPON_NAS_PORT:-2022}"
NAS_USER="${COUPON_NAS_USER:-joon_admin}"
NAS_KEY="${COUPON_NAS_KEY:-$HOME/.ssh/coupang_coupon_nas_actions}"
NAS_STATE_PATH="${COUPON_NAS_STATE_PATH:-/volume1/docker/coupang_coupon/data/wing_storage_state.server.json}"
LOCAL_CONFIG="${COUPON_LOCAL_CONFIG:-browser_coupon_config.json}"
SETUP_TIMEOUT_MINUTES="${COUPON_SETUP_LOGIN_TIMEOUT_MINUTES:-30}"
CHROME_SESSION_PROFILE_DIR="${COUPON_CHROME_SESSION_PROFILE_DIR:-.chrome-wing-login-profile}"
CHROME_REMOTE_DEBUGGING_PORT="${COUPON_CHROME_REMOTE_DEBUGGING_PORT:-9223}"

if [[ ! -f "$NAS_KEY" ]]; then
  echo "[session-refresh] NAS SSH key not found: $NAS_KEY" >&2
  exit 1
fi

if [[ ! -f "$LOCAL_CONFIG" ]]; then
  echo "[session-refresh] Local config not found: $LOCAL_CONFIG" >&2
  exit 1
fi

LOCAL_STATE_PATH="$(python3 - "$LOCAL_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1]).expanduser()
config = json.loads(config_path.read_text(encoding="utf-8"))
state_path = Path(config.get("storage_state_path", "wing_storage_state.json")).expanduser()
if not state_path.is_absolute():
    state_path = config_path.parent / state_path
print(state_path.resolve())
PY
)"

echo "[session-refresh] Step 1/3: 일반 Chrome에서 쿠팡 WING 로그인 세션을 새로 만듭니다."
echo "[session-refresh] 브라우저가 열리면 WING 로그인을 완료한 뒤 터미널에서 Enter를 누르세요."
python3 export_chrome_wing_session.py \
  --config "$LOCAL_CONFIG" \
  --profile-dir "$CHROME_SESSION_PROFILE_DIR" \
  --port "$CHROME_REMOTE_DEBUGGING_PORT" \
  --timeout-seconds "$((SETUP_TIMEOUT_MINUTES * 60))"

if [[ ! -s "$LOCAL_STATE_PATH" ]]; then
  echo "[session-refresh] Storage state was not created: $LOCAL_STATE_PATH" >&2
  exit 1
fi

REMOTE_TMP="${NAS_STATE_PATH}.tmp.$(date +%s)"
SSH_BASE=(
  ssh
  -i "$NAS_KEY"
  -p "$NAS_PORT"
  -o BatchMode=yes
  "$NAS_USER@$NAS_HOST"
)

echo "[session-refresh] Step 2/3: 세션 파일을 NAS로 업로드합니다."
scp -i "$NAS_KEY" -P "$NAS_PORT" "$LOCAL_STATE_PATH" "$NAS_USER@$NAS_HOST:$REMOTE_TMP"
"${SSH_BASE[@]}" "set -e; mv '$REMOTE_TMP' '$NAS_STATE_PATH'; chmod 600 '$NAS_STATE_PATH' || true; rm -f /volume1/docker/coupang_coupon/data/logs/automation_paused.json; /usr/local/bin/docker start coupang-coupon-scheduler >/dev/null 2>&1 || true; ls -lh '$NAS_STATE_PATH'"

echo "[session-refresh] Step 3/3: 업로드 완료."
echo "[session-refresh] NAS 자동화 일시정지를 해제했고, 스케줄러를 다시 켰습니다."
echo "[session-refresh] NAS 자동화는 다음 실행부터 새 세션을 사용합니다."

printf "[session-refresh] 실패/미완료 쿠폰을 지금 복구 실행할까요? [y/N] "
read RUN_RECOVERY
case "$RUN_RECOVERY" in
  y|Y|yes|YES)
    echo "[session-refresh] NAS에서 실패/미완료 쿠폰 복구를 실행합니다."
    "${SSH_BASE[@]}" "set -e; /usr/local/bin/docker exec coupang-coupon-scheduler python3 recover_failed_runner.py"
    ;;
  *)
    echo "[session-refresh] 복구 실행은 건너뜁니다. 웹폼에서도 나중에 실행할 수 있습니다."
    ;;
esac

echo "[session-refresh] 완료."
