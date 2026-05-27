#!/usr/bin/env python3
"""Run all saved Coupang coupons for the current KST date."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import coupon_webapp


ROOT = Path(__file__).resolve().parent
KST = ZoneInfo("Asia/Seoul")
LOG_DIR = coupon_webapp.LOG_DIR
LOCK_PATH = coupon_webapp.DATA_DIR / ".daily_coupon_runner.lock"


def log(message: str) -> None:
    print(f"[daily-coupon] {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run saved Coupang WING coupons for one KST date.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true", help="Actually submit coupons to Coupang WING.")
    mode.add_argument("--dry-run", action="store_true", help="Write the CSV and print the command without submitting.")
    parser.add_argument(
        "--target-date",
        help="Coupon target date YYYY-MM-DD. Defaults to today's date in Asia/Seoul.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    LOG_DIR.mkdir(exist_ok=True)
    target_date = dt.date.fromisoformat(args.target_date) if args.target_date else dt.datetime.now(KST).date()
    log_suffix = "daily_coupon_dry_run" if args.dry_run else "daily_coupon"
    log_path = LOG_DIR / f"{target_date:%Y-%m-%d}_{log_suffix}.log"

    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Another daily coupon run is already active. Exiting.")
            return 0

        coupons, _products = coupon_webapp.load_state()
        if not coupons:
            output = f"[daily-coupon] No saved coupons at {dt.datetime.now(KST).isoformat()}.\n"
            log_path.write_text(output, encoding="utf-8")
            print(output, end="")
            return 0

        coupon_webapp.write_csv(coupons)
        config_path = os.environ.get("COUPON_CONFIG_PATH", "browser_coupon_config.json")
        cmd = [
            sys.executable,
            str(ROOT / "wing_coupon_browser.py"),
            "--config",
            config_path,
            "--csv",
            str(coupon_webapp.GENERATED_CSV_PATH),
            "--target-date",
            str(target_date),
            "--days",
            "1",
            "--submit",
            "--auto-login",
            "--fresh-login",
        ]

        header = (
            "============================================================\n"
            f"Daily coupon run start: {dt.datetime.now(KST).isoformat(timespec='seconds')}\n"
            f"Target date: {target_date}\n"
            f"Coupon count: {len(coupons)}\n"
            f"Command: {' '.join(cmd)}\n"
            "============================================================\n"
        )
        if args.dry_run:
            output = header + "Dry run only. No browser automation was started and no coupon was submitted.\n"
            log_path.write_text(output, encoding="utf-8")
            print(output, end="")
            return 0

        completed = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        footer = (
            "============================================================\n"
            f"Daily coupon run end: {dt.datetime.now(KST).isoformat(timespec='seconds')} / exit={completed.returncode}\n"
            "============================================================\n"
        )
        log_path.write_text(header + completed.stdout + footer, encoding="utf-8")
        print(header + completed.stdout + footer, end="")
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
