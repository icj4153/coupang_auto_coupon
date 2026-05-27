#!/bin/zsh
set -o pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1

mkdir -p logs
LOG_FILE="logs/$(date '+%Y-%m-%d')_coupon_test.log"

{
  echo
  echo "============================================================"
  echo "쿠폰 입력 테스트 시작: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "실제 발급은 하지 않고, 입력과 상품 조회까지만 진행합니다."
  echo "============================================================"
} | tee -a "$LOG_FILE"

python3 wing_coupon_browser.py \
  --config browser_coupon_config.json \
  --csv browser_coupons.csv 2>&1 | tee -a "$LOG_FILE"

STATUS=${pipestatus[1]}

{
  echo "============================================================"
  echo "쿠폰 입력 테스트 종료: $(date '+%Y-%m-%d %H:%M:%S') / exit=$STATUS"
  echo "로그 파일: $LOG_FILE"
  echo "============================================================"
  echo
} | tee -a "$LOG_FILE"

echo "Enter 키를 누르면 창을 닫을 수 있습니다."
read
exit "$STATUS"

