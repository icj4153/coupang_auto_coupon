#!/bin/zsh
SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1

echo "쿠팡 쿠폰 자동화 웹폼을 시작합니다."
echo "브라우저가 열리지 않으면 http://127.0.0.1:8765 로 접속하세요."
echo "이 창을 닫으면 웹폼도 종료됩니다."
python3 coupon_webapp.py

