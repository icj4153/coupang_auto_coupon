#!/usr/bin/env python3
"""Small NAS/container scheduler for daily Coupang coupon runs."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import coupon_webapp


ROOT = Path(__file__).resolve().parent
KST = ZoneInfo("Asia/Seoul")
STATE_PATH = coupon_webapp.LOG_DIR / ".nas_scheduler_state.json"


def log(message: str) -> None:
    print(f"[nas-scheduler] {message}", flush=True)


def parse_run_time() -> tuple[int, int]:
    value = os.environ.get("COUPON_DAILY_TIME", "22:30").strip()
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise SystemExit(f"Invalid COUPON_DAILY_TIME: {value}. Use HH:MM.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SystemExit(f"Invalid COUPON_DAILY_TIME: {value}. Use HH:MM.")
    return hour, minute


def load_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_daily(target_date: dt.date) -> int:
    cmd = [
        sys.executable,
        str(ROOT / "daily_coupon_runner.py"),
        "--run",
        "--target-date",
        str(target_date),
    ]
    log(f"Starting daily coupon run for {target_date}: {' '.join(cmd)}")
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(completed.stdout, end="", flush=True)
    log(f"Daily coupon run finished with exit={completed.returncode}")
    return completed.returncode


def main() -> int:
    coupon_webapp.LOG_DIR.mkdir(parents=True, exist_ok=True)
    hour, minute = parse_run_time()
    state = load_state()
    log(f"Scheduler started. Daily run time: {hour:02d}:{minute:02d} Asia/Seoul")
    log("Each run creates coupons for the next KST date.")

    while True:
        now = dt.datetime.now(KST)
        today = now.date()
        target_date = today + dt.timedelta(days=1)
        target_at = dt.datetime.combine(today, dt.time(hour, minute), tzinfo=KST)
        today_key = today.isoformat()
        target_key = target_date.isoformat()

        if now >= target_at and state.get("last_target_date") != target_key:
            state["last_attempt_date"] = today_key
            state["last_target_date"] = target_key
            state["last_attempt_at"] = now.isoformat(timespec="seconds")
            save_state(state)
            exit_code = run_daily(target_date)
            state["last_exit_code"] = str(exit_code)
            state["last_finished_at"] = dt.datetime.now(KST).isoformat(timespec="seconds")
            save_state(state)

        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
