#!/usr/bin/env python3
"""Run the currently recoverable failed/pending Coupang coupons."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import coupon_webapp


ROOT = Path(__file__).resolve().parent


def main() -> int:
    coupons, _products = coupon_webapp.load_state()
    target_date, pending = coupon_webapp.recoverable_coupons(coupons)
    if not target_date or not pending:
        print("[recover-failed] 복구할 실패/미완료 쿠폰이 없습니다.", flush=True)
        return 0

    print(
        f"[recover-failed] {target_date} 대상 실패/미완료 쿠폰 {len(pending)}개를 실행합니다.",
        flush=True,
    )
    return subprocess.call(
        [
            sys.executable,
            str(ROOT / "daily_coupon_runner.py"),
            "--run",
            "--target-date",
            str(target_date),
        ],
        cwd=ROOT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
