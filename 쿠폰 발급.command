#!/bin/zsh
set -o pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1

mkdir -p logs
LOG_FILE="logs/$(date '+%Y-%m-%d')_coupon_submit.log"

echo "쿠폰을 실제로 발급합니다."
echo "browser_coupons.csv의 행사명, 옵션ID, 할인 조건을 확인했나요?"
printf "진짜 발급하려면 y 를 입력하고 Enter를 누르세요: "
read ANSWER

if [[ "$ANSWER" != "y" && "$ANSWER" != "Y" ]]; then
  echo "발급을 취소했습니다."
  echo "Enter 키를 누르면 창을 닫을 수 있습니다."
  read
  exit 0
fi

{
  echo
  echo "============================================================"
  echo "쿠폰 발급 시작: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "실제 발급 모드입니다."
  echo "============================================================"
} | tee -a "$LOG_FILE"

python3 wing_coupon_browser.py \
  --config browser_coupon_config.json \
  --csv browser_coupons.csv \
  --submit 2>&1 | tee -a "$LOG_FILE"

STATUS=${pipestatus[1]}

{
  echo "============================================================"
  echo "쿠폰 발급 종료: $(date '+%Y-%m-%d %H:%M:%S') / exit=$STATUS"
  echo "로그 파일: $LOG_FILE"
  echo "============================================================"
  echo
} | tee -a "$LOG_FILE"

echo "Enter 키를 누르면 창을 닫을 수 있습니다."
read
exit "$STATUS"

