#!/usr/bin/env python3
"""Small NAS/container scheduler for daily Coupang coupon runs."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import coupon_webapp


ROOT = Path(__file__).resolve().parent
KST = ZoneInfo("Asia/Seoul")
SLOT_GRACE_MINUTES = 18


def log(message: str) -> None:
    print(f"[nas-scheduler] {message}", flush=True)


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


def due_slots(now: dt.datetime) -> list[tuple[dt.date, dt.datetime]]:
    candidates = []
    for target_date in [now.date(), now.date() + dt.timedelta(days=1)]:
        for slot_at in coupon_webapp.scheduled_slots_for_target(target_date):
            if slot_at > now:
                continue
            if now - slot_at > dt.timedelta(minutes=SLOT_GRACE_MINUTES):
                continue
            candidates.append((target_date, slot_at))
    return sorted(candidates, key=lambda item: item[1])


def is_final_slot(target_date: dt.date, slot_at: dt.datetime) -> bool:
    slots = coupon_webapp.scheduled_slots_for_target(target_date)
    return bool(slots and slot_at == slots[-1])


def main() -> int:
    coupon_webapp.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log("Scheduler started. Retry slots: previous day 22:30/22:50/23:10/23:30/23:50, same day 00:05/01:05/02:05/04:05/08:05/12:05 Asia/Seoul")
    log("Each run creates only coupons that are not yet marked successful.")
    pause_logged = False

    while True:
        pause_info = coupon_webapp.automation_pause_info()
        if pause_info:
            if not pause_logged:
                log(f"Automation is paused: {pause_info.get('reason', '')}")
                log("Run refresh_wing_session.command after Coupang WING access works again.")
                pause_logged = True
            time.sleep(30)
            continue
        pause_logged = False
        now = dt.datetime.now(KST)
        coupons, _products = coupon_webapp.load_state()
        for target_date, slot_at in due_slots(now):
            if coupon_webapp.scheduler_slot_was_started(target_date, slot_at):
                continue
            pending = coupon_webapp.pending_coupons_for_date(target_date, coupons)
            if not pending:
                continue
            coupon_webapp.mark_scheduler_slot_started(target_date, slot_at)
            exit_code = run_daily(target_date)
            coupon_webapp.mark_scheduler_slot_finished(target_date, slot_at, exit_code)
            if is_final_slot(target_date, slot_at):
                coupon_webapp.notify_final_failure(target_date, coupons)
            break
        today_slots = coupon_webapp.scheduled_slots_for_target(now.date())
        if today_slots and now >= today_slots[-1] and coupon_webapp.has_run_status_for_date(now.date()):
            if coupon_webapp.pending_coupons_for_date(now.date(), coupons):
                coupon_webapp.notify_final_failure(now.date(), coupons)

        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
