#!/usr/bin/env python3
"""Local web UI for running the Coupang WING coupon automation."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import fcntl
import hmac
import html
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import wing_credentials


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("COUPON_DATA_DIR", ROOT)).expanduser()
LEGACY_SETTINGS_PATH = DATA_DIR / "coupon_form_settings.json"
COUPONS_PATH = DATA_DIR / "coupon_form_coupons.json"
PRODUCTS_PATH = DATA_DIR / "coupon_products.json"
GENERATED_CSV_PATH = DATA_DIR / "browser_coupons.generated.csv"
LOG_DIR = Path(os.environ.get("COUPON_LOG_DIR", DATA_DIR / "logs")).expanduser()
RUN_STATUS_PATH = LOG_DIR / "coupon_run_status.json"
AUTOMATION_PAUSE_PATH = LOG_DIR / "automation_paused.json"
RUN_LOCK_PATH = DATA_DIR / ".coupon_run.lock"
KST = ZoneInfo("Asia/Seoul")
RESULTS_BEGIN = "__WING_COUPON_RESULTS_BEGIN__"
RESULTS_END = "__WING_COUPON_RESULTS_END__"
PREVIOUS_DAY_RETRY_TIMES = ("22:30", "22:50", "23:10", "23:30", "23:50")
SAME_DAY_RETRY_TIMES = ("00:05", "01:05", "02:05", "04:05", "08:05", "12:05")

DEFAULT_COUPON = {
    "campaign_name": "오늘만 특가",
    "coupon_kind": "downloadable",
    "discount_type": "PRICE",
    "product_id": "",
    "vendor_item_ids": "123456",
    "discount": "2000",
    "min_order_price": "5000",
    "max_discount_price": "",
    "max_issue_count": "2",
}

DEFAULT_PRODUCT = {
    "name": "대표 상품",
    "vendor_item_ids": "123456",
}

COUPON_KIND_LABELS = {
    "downloadable": "다운로드쿠폰",
    "instant": "즉시할인쿠폰",
}

DISCOUNT_TYPE_LABELS = {
    "PRICE": "정액",
    "RATE": "정률",
}


def web_auth_credentials() -> tuple[str, str]:
    return (
        os.environ.get("COUPON_WEB_USER", "").strip(),
        os.environ.get("COUPON_WEB_PASSWORD", ""),
    )


def web_auth_enabled() -> bool:
    username, password = web_auth_credentials()
    return bool(username and password)


def valid_basic_auth(header: str | None) -> bool:
    if not web_auth_enabled() or not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode("utf-8")
    except Exception:
        return False
    username, separator, password = decoded.partition(":")
    expected_username, expected_password = web_auth_credentials()
    return (
        bool(separator)
        and hmac.compare_digest(username, expected_username)
        and hmac.compare_digest(password, expected_password)
    )


def now_stamp() -> str:
    return dt.datetime.now(KST).strftime("%Y%m%d%H%M%S")


def new_coupon_id() -> str:
    return f"c{now_stamp()}{uuid.uuid4().hex[:6]}"


def new_product_id() -> str:
    return f"p{now_stamp()}{uuid.uuid4().hex[:6]}"


def normalize_option_ids(value: str) -> str:
    item_lines = []
    for part in value.replace(",", "\n").replace(";", "\n").splitlines():
        part = part.strip()
        if part:
            item_lines.append(part)
    return "\n".join(dict.fromkeys(item_lines))


def normalize_coupon(raw: dict[str, str]) -> dict[str, str]:
    coupon = DEFAULT_COUPON.copy()
    for key in DEFAULT_COUPON:
        if key in raw:
            coupon[key] = str(raw[key]).strip()
    coupon["id"] = str(raw.get("id") or new_coupon_id()).strip()
    coupon["created_at"] = str(raw.get("created_at") or dt.datetime.now(KST).isoformat(timespec="seconds"))

    coupon["coupon_kind"] = coupon["coupon_kind"].lower()
    if coupon["coupon_kind"] not in COUPON_KIND_LABELS:
        coupon["coupon_kind"] = DEFAULT_COUPON["coupon_kind"]

    coupon["discount_type"] = coupon["discount_type"].upper()
    if coupon["discount_type"] not in DISCOUNT_TYPE_LABELS:
        coupon["discount_type"] = DEFAULT_COUPON["discount_type"]

    coupon["product_id"] = str(raw.get("product_id") or coupon.get("product_id") or "").strip()
    coupon["vendor_item_ids"] = normalize_option_ids(coupon["vendor_item_ids"])
    return coupon


def normalize_product(raw: dict[str, str]) -> dict[str, str]:
    product = DEFAULT_PRODUCT.copy()
    for key in DEFAULT_PRODUCT:
        if key in raw:
            product[key] = str(raw[key]).strip()
    product["id"] = str(raw.get("id") or new_product_id()).strip()
    product["created_at"] = str(raw.get("created_at") or dt.datetime.now(KST).isoformat(timespec="seconds"))
    product["name"] = product["name"].strip() or DEFAULT_PRODUCT["name"]
    product["vendor_item_ids"] = normalize_option_ids(product["vendor_item_ids"])
    return product


def load_legacy_coupon() -> list[dict[str, str]]:
    if not LEGACY_SETTINGS_PATH.exists():
        return []
    try:
        with LEGACY_SETTINGS_PATH.open(encoding="utf-8") as handle:
            return [normalize_coupon(json.load(handle))]
    except Exception:
        return []


def load_coupons() -> list[dict[str, str]]:
    if COUPONS_PATH.exists():
        try:
            with COUPONS_PATH.open(encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                return [normalize_coupon(item) for item in data if isinstance(item, dict)]
        except Exception:
            return []

    coupons = load_legacy_coupon()
    if coupons:
        save_coupons(coupons)
    return coupons


def save_coupons(coupons: list[dict[str, str]]) -> None:
    COUPONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with COUPONS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(coupons, handle, ensure_ascii=False, indent=2)


def load_products() -> list[dict[str, str]]:
    if PRODUCTS_PATH.exists():
        try:
            with PRODUCTS_PATH.open(encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                return [normalize_product(item) for item in data if isinstance(item, dict)]
        except Exception:
            return []
    return []


def save_products(products: list[dict[str, str]]) -> None:
    PRODUCTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PRODUCTS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(products, handle, ensure_ascii=False, indent=2)


def product_by_id(products: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {product["id"]: product for product in products}


def product_option_signature(product: dict[str, str]) -> str:
    return product["vendor_item_ids"].strip()


def migrate_coupon_products(
    coupons: list[dict[str, str]],
    products: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], bool]:
    changed = False
    products_by_id = product_by_id(products)
    products_by_options = {
        product_option_signature(product): product
        for product in products
        if product_option_signature(product)
    }

    for coupon in coupons:
        product = products_by_id.get(coupon.get("product_id", ""))
        if product:
            if coupon["vendor_item_ids"] != product["vendor_item_ids"]:
                coupon["vendor_item_ids"] = product["vendor_item_ids"]
                changed = True
            continue

        signature = coupon["vendor_item_ids"].strip()
        if not signature:
            continue

        product = products_by_options.get(signature)
        if not product:
            product = normalize_product(
                {
                    "name": f"{coupon['campaign_name']} 상품",
                    "vendor_item_ids": signature,
                }
            )
            products.append(product)
            products_by_id[product["id"]] = product
            products_by_options[signature] = product
            changed = True

        if coupon.get("product_id") != product["id"]:
            coupon["product_id"] = product["id"]
            changed = True

    return coupons, products, changed


def load_state() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    coupons = load_coupons()
    products = load_products()
    coupons, products, changed = migrate_coupon_products(coupons, products)
    if changed:
        save_coupons(coupons)
        save_products(products)
    return coupons, products


def selected_coupons(coupons: list[dict[str, str]], selected_ids: list[str]) -> list[dict[str, str]]:
    selected = set(selected_ids)
    return [coupon for coupon in coupons if coupon["id"] in selected]


def iso_now() -> str:
    return dt.datetime.now(KST).isoformat(timespec="seconds")


def coupon_key(coupon: dict[str, str]) -> str:
    return str(coupon.get("id") or coupon["campaign_name"]).strip()


def env_truthy(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def error_looks_like_access_denied(value: object) -> bool:
    text = str(value or "").lower()
    return (
        "access denied" in text
        or "login session expired" in text
        or "wing login redirect returned access denied" in text
    )


def automation_pause_info() -> dict[str, object] | None:
    if not AUTOMATION_PAUSE_PATH.exists():
        return None
    try:
        with AUTOMATION_PAUSE_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {"reason": "unknown", "created_at": ""}
    return data if isinstance(data, dict) else {"reason": "unknown", "created_at": ""}


def automation_is_paused() -> bool:
    return automation_pause_info() is not None


def pause_automation(reason: str, *, target_date: dt.date | None = None, log_path: Path | None = None) -> None:
    if not env_truthy("COUPON_PAUSE_ON_ACCESS_DENIED", True):
        return
    AUTOMATION_PAUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": iso_now(),
        "reason": reason,
        "target_date": str(target_date) if target_date else "",
        "log": log_path.name if log_path else "",
    }
    with AUTOMATION_PAUSE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def clear_automation_pause() -> None:
    try:
        AUTOMATION_PAUSE_PATH.unlink()
    except FileNotFoundError:
        pass


def load_run_status() -> dict[str, object]:
    if not RUN_STATUS_PATH.exists():
        return {"dates": {}}
    try:
        with RUN_STATUS_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {"dates": {}}
    if not isinstance(data, dict):
        return {"dates": {}}
    dates = data.get("dates")
    if not isinstance(dates, dict):
        data["dates"] = {}
    return data


def save_run_status(state: dict[str, object]) -> None:
    RUN_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_STATUS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)


def clear_run_status_for_coupon_ids(coupon_ids: list[str], *, from_date: dt.date | None = None) -> int:
    ids = {coupon_id for coupon_id in coupon_ids if coupon_id}
    if not ids:
        return 0
    from_date = from_date or dt.datetime.now(KST).date()
    state = load_run_status()
    dates = state.get("dates", {})
    if not isinstance(dates, dict):
        return 0

    removed = 0
    for date_key, entry in dates.items():
        try:
            target_date = dt.date.fromisoformat(str(date_key))
        except ValueError:
            continue
        if target_date < from_date or not isinstance(entry, dict):
            continue
        records = entry.get("coupons", {})
        if not isinstance(records, dict):
            continue
        entry_removed = 0
        for coupon_id in list(ids):
            if coupon_id in records:
                records.pop(coupon_id, None)
                entry_removed += 1
        if entry_removed:
            removed += entry_removed
            entry["completed"] = False
    if removed:
        save_run_status(state)
    return removed


def run_date_entry(state: dict[str, object], target_date: dt.date) -> dict[str, object]:
    dates = state.setdefault("dates", {})
    if not isinstance(dates, dict):
        dates = {}
        state["dates"] = dates
    key = target_date.isoformat()
    entry = dates.setdefault(
        key,
        {
            "target_date": key,
            "completed": False,
            "coupons": {},
            "run_slots": {},
            "notifications": {},
        },
    )
    if not isinstance(entry, dict):
        entry = {"target_date": key}
        dates[key] = entry
    entry.setdefault("target_date", key)
    entry.setdefault("completed", False)
    entry.setdefault("coupons", {})
    entry.setdefault("run_slots", {})
    entry.setdefault("notifications", {})
    return entry


def coupon_record(entry: dict[str, object], coupon: dict[str, str]) -> dict[str, object]:
    records = entry.setdefault("coupons", {})
    if not isinstance(records, dict):
        records = {}
        entry["coupons"] = records
    key = coupon_key(coupon)
    record = records.setdefault(
        key,
        {
            "coupon_id": key,
            "campaign_name": coupon["campaign_name"],
            "status": "pending",
            "attempt_count": 0,
            "last_error": "",
            "last_log": "",
        },
    )
    if not isinstance(record, dict):
        record = {}
        records[key] = record
    record["coupon_id"] = key
    record["campaign_name"] = coupon["campaign_name"]
    return record


def coupon_status_for_date(
    target_date: dt.date,
    coupon: dict[str, str],
    *,
    state: dict[str, object] | None = None,
) -> str:
    state = state or load_run_status()
    entry = state.get("dates", {}).get(target_date.isoformat(), {})
    if not isinstance(entry, dict):
        return "pending"
    records = entry.get("coupons", {})
    if not isinstance(records, dict):
        return "pending"
    record = records.get(coupon_key(coupon), {})
    if not isinstance(record, dict):
        return "pending"
    status = str(record.get("status") or "pending")
    return status if status in {"success", "failed", "pending", "skipped"} else "pending"


def pending_coupons_for_date(target_date: dt.date, coupons: list[dict[str, str]]) -> list[dict[str, str]]:
    state = load_run_status()
    return [
        coupon
        for coupon in coupons
        if coupon_status_for_date(target_date, coupon, state=state) != "success"
    ]


def skipped_success_results(target_date: dt.date, coupons: list[dict[str, str]]) -> list[dict[str, object]]:
    return [
        {
            "coupon_id": coupon_key(coupon),
            "campaign_name": coupon["campaign_name"],
            "target_date": str(target_date),
            "submit": True,
            "vendor_item_count": option_count(coupon),
            "status": "skipped",
            "error": "이미 성공 기록된 쿠폰이라 중복 생성을 방지했습니다.",
        }
        for coupon in coupons
    ]


def split_already_successful(
    target_date: dt.date,
    coupons: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    state = load_run_status()
    pending = []
    skipped = []
    for coupon in coupons:
        if coupon_status_for_date(target_date, coupon, state=state) == "success":
            skipped.append(coupon)
        else:
            pending.append(coupon)
    return pending, skipped


def result_coupon_key(
    result: dict[str, object],
    coupons_by_id: dict[str, dict[str, str]],
    coupons_by_name: dict[str, dict[str, str]],
) -> str | None:
    raw_id = str(result.get("coupon_id") or "").strip()
    if raw_id in coupons_by_id:
        return raw_id
    name = str(result.get("campaign_name") or "").strip()
    coupon = coupons_by_name.get(name)
    return coupon_key(coupon) if coupon else None


def run_status_summary(
    target_date: dt.date,
    coupons: list[dict[str, str]],
    *,
    state: dict[str, object] | None = None,
) -> dict[str, object]:
    state = state or load_run_status()
    entry = state.get("dates", {}).get(target_date.isoformat(), {})
    records = entry.get("coupons", {}) if isinstance(entry, dict) else {}
    if not isinstance(records, dict):
        records = {}
    success = failed = pending = 0
    failed_names = []
    pending_names = []
    for coupon in coupons:
        record = records.get(coupon_key(coupon), {})
        status = str(record.get("status") if isinstance(record, dict) else "pending") or "pending"
        if status == "success":
            success += 1
        elif status == "failed":
            failed += 1
            failed_names.append(coupon["campaign_name"])
        else:
            pending += 1
            pending_names.append(coupon["campaign_name"])
    total = len(coupons)
    return {
        "target_date": str(target_date),
        "total": total,
        "success": success,
        "failed": failed,
        "pending": pending,
        "unfinished": failed + pending,
        "completed": bool(total and success == total),
        "failed_names": failed_names,
        "pending_names": pending_names,
        "last_log": entry.get("last_log", "") if isinstance(entry, dict) else "",
        "last_run_at": entry.get("last_run_at", "") if isinstance(entry, dict) else "",
        "last_exit_code": entry.get("last_exit_code", "") if isinstance(entry, dict) else "",
    }


def record_run_results(
    target_date: dt.date,
    attempted_coupons: list[dict[str, str]],
    results: list[dict[str, object]],
    *,
    log_path: Path,
    returncode: int,
    source: str,
    all_coupons: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    state = load_run_status()
    all_coupons = all_coupons or attempted_coupons
    previous = run_status_summary(target_date, all_coupons, state=state)
    entry = run_date_entry(state, target_date)
    coupons_by_id = {coupon_key(coupon): coupon for coupon in attempted_coupons}
    coupons_by_name = {coupon["campaign_name"]: coupon for coupon in attempted_coupons}
    results_by_id: dict[str, dict[str, object]] = {}
    for result in results:
        key = result_coupon_key(result, coupons_by_id, coupons_by_name)
        if key:
            results_by_id[key] = result

    for coupon in all_coupons:
        coupon_record(entry, coupon)

    for coupon in attempted_coupons:
        record = coupon_record(entry, coupon)
        result = results_by_id.get(coupon_key(coupon), {})
        status = str(result.get("status") or "failed")
        if status == "skipped":
            continue
        record["status"] = "success" if status == "success" else "failed"
        record["attempt_count"] = int(record.get("attempt_count") or 0) + 1
        record["last_attempt_at"] = iso_now()
        record["last_log"] = log_path.name
        record["last_error"] = "" if record["status"] == "success" else str(result.get("error") or "쿠폰 생성 실패")
        record["vendor_item_count"] = option_count(coupon)
        if result.get("start_at"):
            record["start_at"] = str(result["start_at"])

    if any(error_looks_like_access_denied(result.get("error")) for result in results):
        pause_automation(
            "WING Access Denied 또는 로그인 세션 만료가 감지되어 자동 재시도를 중지했습니다.",
            target_date=target_date,
            log_path=log_path,
        )

    entry["last_run_at"] = iso_now()
    entry["last_log"] = log_path.name
    entry["last_exit_code"] = str(returncode)
    entry["last_source"] = source
    summary = run_status_summary(target_date, all_coupons, state=state)
    entry["completed"] = summary["completed"]
    save_run_status(state)
    summary["previous_unfinished"] = previous["unfinished"]
    return summary


def parse_clock(value: str) -> dt.time:
    hour, minute = value.split(":", 1)
    return dt.time(int(hour), int(minute))


def scheduled_slots_for_target(target_date: dt.date) -> list[dt.datetime]:
    previous_day = target_date - dt.timedelta(days=1)
    slots = [
        dt.datetime.combine(previous_day, parse_clock(value), tzinfo=KST)
        for value in PREVIOUS_DAY_RETRY_TIMES
    ]
    slots.extend(
        dt.datetime.combine(target_date, parse_clock(value), tzinfo=KST)
        for value in SAME_DAY_RETRY_TIMES
    )
    return sorted(slots)


def next_retry_time(target_date: dt.date, coupons: list[dict[str, str]]) -> str:
    summary = run_status_summary(target_date, coupons)
    if summary["completed"]:
        return "완료"
    now = dt.datetime.now(KST)
    state = load_run_status()
    entry = run_date_entry(state, target_date)
    run_slots = entry.get("run_slots", {})
    if not isinstance(run_slots, dict):
        run_slots = {}
    for slot_at in scheduled_slots_for_target(target_date):
        slot_key = slot_at.isoformat(timespec="minutes")
        if slot_at > now and slot_key not in run_slots:
            return slot_at.strftime("%m/%d %H:%M")
    return "수동 복구 필요"


def mark_scheduler_slot_started(target_date: dt.date, slot_at: dt.datetime) -> None:
    state = load_run_status()
    entry = run_date_entry(state, target_date)
    run_slots = entry.setdefault("run_slots", {})
    if not isinstance(run_slots, dict):
        run_slots = {}
        entry["run_slots"] = run_slots
    run_slots[slot_at.isoformat(timespec="minutes")] = {
        "started_at": iso_now(),
        "exit_code": "",
    }
    save_run_status(state)


def mark_scheduler_slot_finished(target_date: dt.date, slot_at: dt.datetime, exit_code: int) -> None:
    state = load_run_status()
    entry = run_date_entry(state, target_date)
    run_slots = entry.setdefault("run_slots", {})
    if not isinstance(run_slots, dict):
        run_slots = {}
        entry["run_slots"] = run_slots
    record = run_slots.setdefault(slot_at.isoformat(timespec="minutes"), {})
    if isinstance(record, dict):
        record["finished_at"] = iso_now()
        record["exit_code"] = str(exit_code)
    save_run_status(state)


def scheduler_slot_was_started(target_date: dt.date, slot_at: dt.datetime) -> bool:
    state = load_run_status()
    entry = state.get("dates", {}).get(target_date.isoformat(), {})
    if not isinstance(entry, dict):
        return False
    run_slots = entry.get("run_slots", {})
    return isinstance(run_slots, dict) and slot_at.isoformat(timespec="minutes") in run_slots


def has_run_status_for_date(target_date: dt.date) -> bool:
    state = load_run_status()
    dates = state.get("dates", {})
    if not isinstance(dates, dict):
        return False
    entry = dates.get(target_date.isoformat())
    return isinstance(entry, dict)


def send_wake_on_lan(mac_address: str) -> bool:
    clean = mac_address.replace("-", "").replace(":", "").replace(".", "").strip()
    if len(clean) != 12:
        print(f"[wake] 잘못된 MAC 주소 형식입니다: {mac_address}", flush=True)
        return False
    try:
        payload = bytes.fromhex("ff" * 6 + clean * 16)
    except ValueError:
        print(f"[wake] 잘못된 MAC 주소 형식입니다: {mac_address}", flush=True)
        return False
    broadcast = os.environ.get("MAC_WAKE_BROADCAST", "192.168.50.255").strip() or "255.255.255.255"
    port = int(os.environ.get("MAC_WAKE_PORT", "9") or "9")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(payload, (broadcast, port))
        print(f"[wake] Wake-on-LAN packet sent to {broadcast}:{port}.", flush=True)
        return True
    except OSError as exc:
        print(f"[wake] Wake-on-LAN 전송 실패: {exc}", flush=True)
        return False


def wake_mac_if_configured() -> bool:
    if not env_truthy("MAC_WAKE_ON_FAILURE", False):
        return False
    mac_address = os.environ.get("MAC_WAKE_ADDRESS", "").strip()
    if not mac_address:
        print("[wake] MAC_WAKE_ON_FAILURE=true 이지만 MAC_WAKE_ADDRESS가 비어 있습니다.", flush=True)
        return False
    return send_wake_on_lan(mac_address)


def send_telegram_message(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[telegram] 알림 전송 실패: {exc}", flush=True)
        return False


def notify_run_event(
    target_date: dt.date,
    coupons: list[dict[str, str]],
    event_key: str,
    title: str,
) -> bool:
    state = load_run_status()
    entry = run_date_entry(state, target_date)
    notifications = entry.setdefault("notifications", {})
    if not isinstance(notifications, dict):
        notifications = {}
        entry["notifications"] = notifications
    if notifications.get(event_key):
        return False
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or not os.environ.get("TELEGRAM_CHAT_ID", "").strip():
        print("[telegram] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 없어 알림을 기록만 하고 건너뜁니다.", flush=True)
        notifications[event_key] = f"telegram_not_configured:{iso_now()}"
        save_run_status(state)
        return False
    summary = run_status_summary(target_date, coupons, state=state)
    failed_names = [*summary["failed_names"], *summary["pending_names"]]
    entry = state.get("dates", {}).get(target_date.isoformat(), {})
    records = entry.get("coupons", {}) if isinstance(entry, dict) else {}
    access_denied = False
    if isinstance(records, dict):
        access_denied = any(
            error_looks_like_access_denied(record.get("last_error", ""))
            for record in records.values()
            if isinstance(record, dict)
        )
    wake_sent = wake_mac_if_configured() if access_denied else False
    lines = [
        f"[쿠팡 쿠폰 자동화] {title}",
        f"대상일: {target_date}",
        f"성공: {summary['success']} / 실패·미완료: {summary['unfinished']} / 전체: {summary['total']}",
    ]
    if failed_names:
        lines.append("대상 쿠폰: " + ", ".join(str(name) for name in failed_names[:8]))
    if summary.get("last_log"):
        lines.append(f"로그: {summary['last_log']}")
    if access_denied:
        lines.append("원인: WING 로그인 Access Denied 또는 세션 만료")
        if wake_sent:
            lines.append("Mac 깨우기: Wake-on-LAN 패킷 전송 완료")
        lines.append("조치: Mac을 깨운 뒤 refresh_wing_session.command 실행")
        lines.append("완료 후 스크립트에서 실패/미완료 쿠폰 복구 실행")
    if not send_telegram_message("\n".join(lines)):
        return False
    notifications[event_key] = iso_now()
    save_run_status(state)
    return True


def notify_after_run(target_date: dt.date, coupons: list[dict[str, str]], summary: dict[str, object]) -> None:
    unfinished = int(summary.get("unfinished") or 0)
    success = int(summary.get("success") or 0)
    previous_unfinished = int(summary.get("previous_unfinished") or 0)
    if unfinished:
        if success:
            notify_run_event(target_date, coupons, "partial_failure", "일부 쿠폰 생성 실패")
        else:
            notify_run_event(target_date, coupons, "first_failure", "쿠폰 자동 생성 실패")
    elif previous_unfinished:
        notify_run_event(target_date, coupons, "recovered", "실패 쿠폰 복구 성공")


def notify_final_failure(target_date: dt.date, coupons: list[dict[str, str]]) -> None:
    summary = run_status_summary(target_date, coupons)
    if int(summary.get("unfinished") or 0):
        notify_run_event(target_date, coupons, "final_failure", "최종 복구 시간 이후에도 실패")


def write_csv(coupons: list[dict[str, str]]) -> None:
    GENERATED_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with GENERATED_CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "coupon_id",
                "enabled",
                "campaign_name",
                "coupon_kind",
                "vendor_item_ids",
                "discount_type",
                "discount",
                "min_order_price",
                "max_discount_price",
                "max_issue_count",
            ],
        )
        writer.writeheader()
        for coupon in coupons:
            writer.writerow(
                {
                    "coupon_id": coupon_key(coupon),
                    "enabled": "true",
                    "campaign_name": coupon["campaign_name"],
                    "coupon_kind": coupon["coupon_kind"],
                    "vendor_item_ids": ";".join(coupon["vendor_item_ids"].splitlines()),
                    "discount_type": coupon["discount_type"],
                    "discount": coupon["discount"],
                    "min_order_price": coupon["min_order_price"],
                    "max_discount_price": coupon["max_discount_price"],
                    "max_issue_count": coupon["max_issue_count"],
                }
            )


def validate_coupon(coupon: dict[str, str]) -> list[str]:
    errors = []
    if not coupon["campaign_name"]:
        errors.append("행사명을 입력하세요.")
    if not coupon["product_id"]:
        errors.append("상품을 선택하세요.")
    if not coupon["vendor_item_ids"]:
        errors.append("선택한 상품에 옵션ID가 없습니다.")
    if not coupon["discount"].isdigit() or int(coupon["discount"]) <= 0:
        label = "할인율" if coupon["discount_type"] == "RATE" else "할인금액"
        errors.append(f"{label}은 1 이상의 숫자로 입력하세요.")
    if coupon["discount_type"] == "RATE" and coupon["discount"].isdigit() and int(coupon["discount"]) > 100:
        errors.append("할인율은 100 이하 숫자로 입력하세요.")
    if coupon["discount_type"] == "RATE" and coupon["max_discount_price"]:
        if not coupon["max_discount_price"].isdigit() or int(coupon["max_discount_price"]) <= 0:
            errors.append("최대 할인금액은 비워두거나 1 이상의 숫자로 입력하세요.")
    if coupon["coupon_kind"] == "downloadable":
        for key, label in [
            ("min_order_price", "최소 구매금액"),
            ("max_issue_count", "최대 발급 개수"),
        ]:
            if not coupon[key].isdigit() or int(coupon[key]) <= 0:
                errors.append(f"{label}은 1 이상의 숫자로 입력하세요.")
    return errors


def validate_product(product: dict[str, str]) -> list[str]:
    errors = []
    if not product["name"]:
        errors.append("상품명을 입력하세요.")
    if not product["vendor_item_ids"]:
        errors.append("옵션ID를 한 개 이상 입력하세요.")
    return errors


def format_number(value: str) -> str:
    if value.isdigit():
        return f"{int(value):,}"
    return value


def option_count(coupon: dict[str, str]) -> int:
    return len([line for line in coupon["vendor_item_ids"].splitlines() if line.strip()])


def option_preview(product: dict[str, str], limit: int = 4) -> str:
    option_ids = [line for line in product["vendor_item_ids"].splitlines() if line.strip()]
    preview = ", ".join(option_ids[:limit])
    if len(option_ids) > limit:
        preview += f" 외 {len(option_ids) - limit}개"
    return preview or "-"


def coupon_summary(coupon: dict[str, str]) -> str:
    kind = COUPON_KIND_LABELS[coupon["coupon_kind"]]
    discount_type = DISCOUNT_TYPE_LABELS[coupon["discount_type"]]
    if coupon["discount_type"] == "RATE":
        if coupon["coupon_kind"] == "downloadable":
            detail = f"{format_number(coupon['min_order_price'])}원 이상 구매 시 {coupon['discount']}% 할인"
        else:
            detail = f"{coupon['discount']}% 할인"
        if coupon["max_discount_price"]:
            detail += f", 최대 {format_number(coupon['max_discount_price'])}원"
    elif coupon["coupon_kind"] == "downloadable":
        detail = f"{format_number(coupon['min_order_price'])}원 이상 구매 시 {format_number(coupon['discount'])}원 할인"
    else:
        detail = f"{format_number(coupon['discount'])}원 할인"
    if coupon["coupon_kind"] == "downloadable":
        detail += f" / 최대 발급 {format_number(coupon['max_issue_count'])}개"
    return f"{kind} / {discount_type} / {detail}"


def parse_automation_results(output: str) -> list[dict[str, object]]:
    start = output.rfind(RESULTS_BEGIN)
    if start < 0:
        return []
    start += len(RESULTS_BEGIN)
    end = output.find(RESULTS_END, start)
    if end < 0:
        return []
    try:
        parsed = json.loads(output[start:end].strip())
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def automation_error_summary(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("ERROR:"):
            return line.removeprefix("ERROR:").strip()
    return lines[-1] if lines else "자동화가 결과를 반환하지 않았습니다."


def fallback_automation_results(
    coupons: list[dict[str, str]],
    *,
    submit: bool,
    returncode: int,
    output: str,
) -> list[dict[str, object]]:
    error = (
        automation_error_summary(output)
        if returncode
        else "자동화는 종료됐지만 쿠폰별 결과를 확인하지 못했습니다. 로그를 확인하세요."
    )
    return [
        {
            "coupon_id": coupon_key(coupon),
            "campaign_name": coupon["campaign_name"],
            "target_date": "",
            "submit": submit,
            "vendor_item_count": option_count(coupon),
            "status": "failed",
            "error": error,
        }
        for coupon in coupons
    ]


def run_automation(
    coupons: list[dict[str, str]],
    submit: bool,
    *,
    target_date: dt.date | None = None,
    start_time: str | None = None,
) -> tuple[int, str, list[dict[str, object]]]:
    LOG_DIR.mkdir(exist_ok=True)
    target_date = target_date or (dt.datetime.now(KST).date() + dt.timedelta(days=1))
    stamp = dt.datetime.now(KST).strftime("%Y-%m-%d_%H%M%S")
    log_path = LOG_DIR / f"{stamp}_{'submit' if submit else 'test'}.log"
    skipped_results: list[dict[str, object]] = []
    runnable_coupons = coupons
    if submit:
        runnable_coupons, skipped_coupons = split_already_successful(target_date, coupons)
        skipped_results = skipped_success_results(target_date, skipped_coupons)
        if not runnable_coupons:
            output = "선택한 쿠폰은 모두 이미 성공 기록이 있어 중복 생성을 건너뛰었습니다.\n"
            log_path.write_text(output, encoding="utf-8")
            return 0, output, skipped_results

    RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            output = "다른 쿠폰 생성 작업이 이미 실행 중입니다. 완료 후 다시 시도하세요.\n"
            log_path.write_text(output, encoding="utf-8")
            return (
                1,
                output,
                skipped_results
                + fallback_automation_results(
                    runnable_coupons,
                    submit=submit,
                    returncode=1,
                    output=output,
                ),
            )

        write_csv(runnable_coupons)
        config_path = os.environ.get("COUPON_CONFIG_PATH", "browser_coupon_config.json")
        cmd = [
            sys.executable,
            str(ROOT / "wing_coupon_browser.py"),
            "--config",
            config_path,
            "--csv",
            str(GENERATED_CSV_PATH),
            "--target-date",
            str(target_date),
            "--days",
            "1",
            "--auto-login",
            "--continue-on-error",
        ]
        if start_time:
            cmd.extend(["--start-time", start_time])
        if submit:
            cmd.append("--submit")
        completed = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    results = parse_automation_results(completed.stdout)
    if not results:
        results = fallback_automation_results(
            runnable_coupons,
            submit=submit,
            returncode=completed.returncode,
            output=completed.stdout,
        )
    for result in results:
        result.setdefault("target_date", str(target_date))
    log_path.write_text(completed.stdout, encoding="utf-8")
    if submit:
        record_run_results(
            target_date,
            runnable_coupons,
            results,
            log_path=log_path,
            returncode=completed.returncode,
            source="manual",
            all_coupons=load_state()[0],
        )
    return completed.returncode, completed.stdout, skipped_results + results


def validate_same_day_start_time(value: str) -> tuple[str, str]:
    value = value.strip()
    try:
        start_clock = dt.time.fromisoformat(value)
    except ValueError:
        return "", "당일 시작 시간을 HH:MM 형식으로 입력하세요."
    normalized = start_clock.strftime("%H:%M")
    now = dt.datetime.now(KST)
    start_at = dt.datetime.combine(now.date(), start_clock, tzinfo=KST)
    if start_at <= now:
        return "", "당일 시작 시간은 현재 시각보다 뒤여야 합니다."
    if start_clock >= dt.time(23, 59):
        return "", "당일 시작 시간은 23:59보다 앞이어야 합니다."
    return normalized, ""


def scheduler_time() -> str:
    return f"{PREVIOUS_DAY_RETRY_TIMES[0]} 시작"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def badge(text: str, class_name: str = "") -> str:
    return f"<span class='badge {class_name}'>{esc(text)}</span>"


def product_rows_html(products: list[dict[str, str]]) -> str:
    if not products:
        return """
        <tr>
          <td colspan="4" class="empty">
            저장된 상품이 없습니다. 상품 추가 버튼으로 옵션ID 묶음을 먼저 등록하세요.
          </td>
        </tr>
        """

    rows = []
    for product in products:
        rows.append(
            f"""
            <tr>
              <td>
                <div class="name">{esc(product['name'])}</div>
                <div class="muted">{esc(option_preview(product))}</div>
              </td>
              <td class="num">{option_count(product)}개</td>
              <td class="date">{esc(product['created_at'][:10])}</td>
              <td class="actions-cell">
                <button class="edit" type="submit" name="edit_product_id" value="{esc(product['id'])}">수정</button>
                <button class="delete" type="submit" name="delete_product_id" value="{esc(product['id'])}" onclick="return confirm('이 상품을 삭제할까요? 사용 중인 쿠폰이 있으면 삭제되지 않습니다.')">삭제</button>
              </td>
            </tr>
            """
        )
    return "\n".join(rows)


def coupon_rows_html(
    coupons: list[dict[str, str]],
    products: list[dict[str, str]],
    target_date: dt.date,
) -> str:
    if not coupons:
        return """
        <tr>
          <td colspan="6" class="empty">
            저장된 쿠폰이 없습니다. 오른쪽 위의 쿠폰 추가 버튼으로 첫 쿠폰을 등록하세요.
          </td>
        </tr>
        """

    rows = []
    products_by_id = product_by_id(products)
    for coupon in coupons:
        kind_class = "download" if coupon["coupon_kind"] == "downloadable" else "instant"
        type_class = "price" if coupon["discount_type"] == "PRICE" else "rate"
        product = products_by_id.get(coupon.get("product_id", ""))
        product_name = product["name"] if product else "상품 없음"
        product_options = option_count(product) if product else option_count(coupon)
        rows.append(
            f"""
            <tr>
              <td class="check-cell">
                <input type="checkbox" name="selected_ids" value="{esc(coupon['id'])}" aria-label="{esc(coupon['campaign_name'])} 선택">
              </td>
              <td>
                <div class="name">{esc(coupon['campaign_name'])}</div>
                <div class="meta">{badge(COUPON_KIND_LABELS[coupon['coupon_kind']], kind_class)} {badge(DISCOUNT_TYPE_LABELS[coupon['discount_type']], type_class)}</div>
              </td>
              <td>
                <div class="name">{esc(product_name)}</div>
                <div class="muted">옵션 {product_options}개</div>
              </td>
              <td>{esc(coupon_summary(coupon))}</td>
              <td class="date">{target_date:%Y-%m-%d}</td>
              <td class="actions-cell">
                <button class="edit" type="submit" name="edit_coupon_id" value="{esc(coupon['id'])}">수정</button>
                <button class="delete" type="submit" name="delete_coupon_id" value="{esc(coupon['id'])}" onclick="return confirm('이 쿠폰을 삭제할까요?')">삭제</button>
              </td>
            </tr>
            """
        )
    return "\n".join(rows)


def modal_form_html(
    coupon: dict[str, str],
    products: list[dict[str, str]],
    show_modal: bool,
    *,
    edit_id: str = "",
) -> str:
    coupon = normalize_coupon(coupon)
    is_edit = bool(edit_id)
    coupon_kind_checked = {
        key: "checked" if coupon["coupon_kind"] == key else ""
        for key in COUPON_KIND_LABELS
    }
    discount_type_checked = {
        key: "checked" if coupon["discount_type"] == key else ""
        for key in DISCOUNT_TYPE_LABELS
    }
    download_hidden = "" if coupon["coupon_kind"] == "downloadable" else "hidden"
    rate_hidden = "" if coupon["discount_type"] == "RATE" else "hidden"
    open_attr = "open" if show_modal else ""
    summary = esc(coupon_summary(coupon))
    product_options = []
    if products:
        product_options.append("<option value=\"\">상품을 선택하세요</option>")
        for product in products:
            selected = "selected" if coupon.get("product_id") == product["id"] else ""
            product_options.append(
                f"<option value=\"{esc(product['id'])}\" {selected}>"
                f"{esc(product['name'])} / 옵션 {option_count(product)}개"
                "</option>"
            )
    else:
        product_options.append("<option value=\"\">먼저 상품을 추가하세요</option>")
    product_select_disabled = "" if products else "disabled"
    action = "update_coupon" if is_edit else "add_coupon"
    title = "쿠폰 수정" if is_edit else "쿠폰 추가"
    description = "조건을 바꾸면 오늘 이후 성공 기록을 초기화합니다." if is_edit else "저장 후 목록에서 선택해 실행합니다."
    hidden_id = f'<input type="hidden" name="coupon_id" value="{esc(edit_id)}">' if is_edit else ""
    submit_label = "수정 저장" if is_edit else "저장"
    return f"""
    <dialog id="coupon-modal" {open_attr}>
      <form id="coupon-form" method="post">
        <input type="hidden" name="action" value="{action}">
        {hidden_id}
        <div class="modal-head">
          <div>
            <h2>{title}</h2>
            <p>{description}</p>
          </div>
          <button class="icon-button" type="button" data-close-modal aria-label="닫기">×</button>
        </div>

        <fieldset>
          <legend>쿠폰 종류</legend>
          <div class="choice-group">
            <label class="choice">
              <input type="radio" name="coupon_kind" value="downloadable" {coupon_kind_checked['downloadable']}>
              다운로드쿠폰
            </label>
            <label class="choice">
              <input type="radio" name="coupon_kind" value="instant" {coupon_kind_checked['instant']}>
              즉시할인쿠폰
            </label>
          </div>
        </fieldset>

        <fieldset>
          <legend>할인 방식</legend>
          <div class="choice-group">
            <label class="choice">
              <input type="radio" name="discount_type" value="PRICE" {discount_type_checked['PRICE']}>
              정액
            </label>
            <label class="choice">
              <input type="radio" name="discount_type" value="RATE" {discount_type_checked['RATE']}>
              정률
            </label>
          </div>
        </fieldset>

        <label for="campaign_name">행사명</label>
        <input id="campaign_name" name="campaign_name" value="{esc(coupon['campaign_name'])}" required>
        <div class="hint">입력한 행사명이 쿠폰명에 그대로 들어갑니다.</div>

        <label for="product_id">상품</label>
        <select id="product_id" name="product_id" required {product_select_disabled}>
          {''.join(product_options)}
        </select>
        <div class="hint">상품 추가에서 등록한 옵션ID 묶음을 선택합니다.</div>

        <div class="grid">
          <div class="download-only" {download_hidden}>
            <label for="min_order_price">최소 구매금액</label>
            <input id="min_order_price" name="min_order_price" inputmode="numeric" value="{esc(coupon['min_order_price'])}">
          </div>
          <div>
            <label id="discount_label" for="discount">할인금액</label>
            <input id="discount" name="discount" inputmode="numeric" value="{esc(coupon['discount'])}" required>
            <div id="discount_hint" class="hint"></div>
          </div>
          <div class="rate-only" {rate_hidden}>
            <label for="max_discount_price">최대 할인금액</label>
            <input id="max_discount_price" name="max_discount_price" inputmode="numeric" value="{esc(coupon['max_discount_price'])}">
            <div class="hint">정률 할인일 때만 사용합니다. 비워두면 입력하지 않습니다.</div>
          </div>
          <div class="download-only" {download_hidden}>
            <label for="max_issue_count">최대 발급 개수</label>
            <input id="max_issue_count" name="max_issue_count" inputmode="numeric" value="{esc(coupon['max_issue_count'])}">
          </div>
        </div>

        <div id="summary" class="summary">{summary}</div>
        <div class="modal-actions">
          <button class="save" type="button" data-close-modal>취소</button>
          <button class="submit" type="submit">{submit_label}</button>
        </div>
      </form>
    </dialog>
    """


def product_modal_html(product: dict[str, str], show_modal: bool, *, edit_id: str = "") -> str:
    product = normalize_product(product)
    is_edit = bool(edit_id)
    open_attr = "open" if show_modal else ""
    action = "update_product" if is_edit else "add_product"
    title = "상품 수정" if is_edit else "상품 추가"
    description = "옵션ID를 바꾸면 연결된 쿠폰의 오늘 이후 성공 기록을 초기화합니다." if is_edit else "옵션ID 묶음을 저장한 뒤 쿠폰에서 선택합니다."
    hidden_id = f'<input type="hidden" name="product_id" value="{esc(edit_id)}">' if is_edit else ""
    submit_label = "수정 저장" if is_edit else "저장"
    return f"""
    <dialog id="product-modal" {open_attr}>
      <form id="product-form" method="post">
        <input type="hidden" name="action" value="{action}">
        {hidden_id}
        <div class="modal-head">
          <div>
            <h2>{title}</h2>
            <p>{description}</p>
          </div>
          <button class="icon-button" type="button" data-close-product-modal aria-label="닫기">×</button>
        </div>

        <label for="product_name">상품명</label>
        <input id="product_name" name="name" value="{esc(product['name'])}" required>
        <div class="hint">예: 수박 4종, 행운특가 상품, 사과 옵션 묶음</div>

        <label for="product_vendor_item_ids">옵션ID</label>
        <textarea id="product_vendor_item_ids" name="vendor_item_ids" required>{esc(product['vendor_item_ids'])}</textarea>
        <div class="hint">여러 개는 줄바꿈으로 붙여넣으세요. 콤마와 세미콜론도 자동으로 줄바꿈 처리됩니다.</div>

        <div class="modal-actions">
          <button class="save" type="button" data-close-product-modal>취소</button>
          <button class="submit" type="submit">{submit_label}</button>
        </div>
      </form>
    </dialog>
    """


def execution_results_html(results: list[dict[str, object]], submit: bool) -> str:
    if not results:
        return ""
    failed_count = sum(1 for result in results if result.get("status") == "failed")
    success_count = len(results) - failed_count
    title = "쿠폰 만들기 결과" if submit else "입력 테스트 결과"
    summary_class = "result-summary failed" if failed_count else "result-summary success"
    summary = f"성공 {success_count}개 · 실패 {failed_count}개"
    rows = []
    for result in results:
        status = str(result.get("status") or "")
        failed = status == "failed"
        skipped = status == "skipped"
        if failed:
            status_label = "실패"
        elif skipped:
            status_label = "건너뜀"
        else:
            status_label = "발급 성공" if submit else "입력 완료"
        status_class = "result-status skipped" if skipped else ("result-status failed" if failed else "result-status success")
        target_date = str(result.get("target_date") or "-")
        start_at = str(result.get("start_at") or "")
        schedule = start_at.replace("T", " ") if start_at else target_date
        detail = str(result.get("error") or ("쿠폰이 정상적으로 발급되었습니다." if submit else "쿠폰 입력과 상품 조회를 완료했습니다."))
        rows.append(
            f"""
            <tr>
              <td data-label="쿠폰명"><strong>{esc(result.get('campaign_name') or '-')}</strong></td>
              <td class="date" data-label="대상 일시">{esc(schedule)}</td>
              <td data-label="결과"><span class="{status_class}">{status_label}</span></td>
              <td class="result-detail" data-label="상세">{esc(detail)}</td>
            </tr>
            """
        )
    return f"""
    <section class="execution-results" aria-live="polite">
      <div class="result-head">
        <strong>{title}</strong>
        <span class="{summary_class}">{summary}</span>
      </div>
      <div class="result-table-wrap">
        <table>
          <thead><tr><th>쿠폰명</th><th>대상 일시</th><th>결과</th><th>상세</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </section>
    """


def status_label(summary: dict[str, object]) -> tuple[str, str]:
    if summary["completed"]:
        return "완료", "success"
    if int(summary.get("failed") or 0):
        return "실패 있음", "failed"
    return "미완료", "pending"


def run_status_card_html(label: str, target_date: dt.date, coupons: list[dict[str, str]]) -> str:
    summary = run_status_summary(target_date, coupons)
    status_text, status_class = status_label(summary)
    unfinished_names = [*summary["failed_names"], *summary["pending_names"]]
    names_text = ", ".join(str(name) for name in unfinished_names[:4])
    if len(unfinished_names) > 4:
        names_text += f" 외 {len(unfinished_names) - 4}개"
    if not names_text:
        names_text = "미완료 쿠폰 없음"
    next_time = next_retry_time(target_date, coupons)
    return f"""
    <div class="status-card {status_class}">
      <div class="status-card-head">
        <strong>{esc(label)} {target_date:%m/%d}</strong>
        <span>{esc(status_text)}</span>
      </div>
      <div class="status-metrics">
        <span>성공 {summary['success']}</span>
        <span>실패 {summary['failed']}</span>
        <span>미완료 {summary['pending']}</span>
      </div>
      <div class="muted">다음 재시도: {esc(next_time)}</div>
      <div class="muted">대상: {esc(names_text)}</div>
    </div>
    """


def recoverable_coupons(coupons: list[dict[str, str]]) -> tuple[dt.date | None, list[dict[str, str]]]:
    today = dt.datetime.now(KST).date()
    for target_date in [today, today + dt.timedelta(days=1)]:
        pending = pending_coupons_for_date(target_date, coupons)
        if pending:
            return target_date, pending
    return None, []


def operation_status_html(coupons: list[dict[str, str]]) -> str:
    today = dt.datetime.now(KST).date()
    tomorrow = today + dt.timedelta(days=1)
    recover_date, pending = recoverable_coupons(coupons)
    disabled = "disabled" if not pending else ""
    button_text = "실패/미완료 쿠폰만 지금 복구 실행"
    hint = (
        f"{recover_date:%Y-%m-%d} 대상 {len(pending)}개를 실행합니다."
        if recover_date
        else "복구할 실패/미완료 쿠폰이 없습니다."
    )
    return f"""
    <section class="operation-status panel">
      <div class="panel-head">
        <div>
          <strong>자동 생성 상태</strong>
          <div class="muted">성공한 쿠폰은 다시 만들지 않고 실패/미완료 쿠폰만 재시도합니다.</div>
        </div>
        <form method="post" class="inline-form">
          <input type="hidden" name="action" value="recover_failed">
          <button class="today-submit" type="submit" {disabled} onclick="return confirm('실패/미완료 쿠폰만 실제 발급할까요?')">{button_text}</button>
        </form>
      </div>
      <div class="status-grid">
        {run_status_card_html("오늘", today, coupons)}
        {run_status_card_html("내일", tomorrow, coupons)}
      </div>
      <div class="status-foot muted">{esc(hint)}</div>
    </section>
    """


def page_html(
    coupons: list[dict[str, str]],
    products: list[dict[str, str]],
    message: str = "",
    output: str = "",
    *,
    modal_coupon: dict[str, str] | None = None,
    show_modal: bool = False,
    modal_coupon_edit_id: str = "",
    modal_product: dict[str, str] | None = None,
    show_product_modal: bool = False,
    modal_product_edit_id: str = "",
    same_day_start_time: str = "",
    execution_results: list[dict[str, object]] | None = None,
    execution_submit: bool = False,
) -> bytes:
    today = dt.datetime.now(KST).date()
    target_date = today + dt.timedelta(days=1)
    execution_results = execution_results or []
    has_failures = any(result.get("status") == "failed" for result in execution_results)
    notice_class = "notice danger" if has_failures else ("notice success" if execution_results else "notice")
    message_html = f"<div class='{notice_class}'>{esc(message)}</div>" if message else ""
    results_html = execution_results_html(execution_results, execution_submit)
    output_html = f"<pre>{esc(output[-10000:])}</pre>" if output else ""
    coupon_count = len(coupons)
    product_count = len(products)
    option_total = sum(option_count(product) for product in products)
    modal_html = modal_form_html(
        modal_coupon or DEFAULT_COUPON,
        products,
        show_modal,
        edit_id=modal_coupon_edit_id,
    )
    product_modal = product_modal_html(
        modal_product or DEFAULT_PRODUCT,
        show_product_modal,
        edit_id=modal_product_edit_id,
    )
    product_rows = product_rows_html(products)
    rows_html = coupon_rows_html(coupons, products, target_date)
    operation_status = operation_status_html(coupons)
    credential_source = wing_credentials.credential_source()
    credential_status = {
        "environment": "환경변수",
    }.get(credential_source, "미설정")
    schedule_status = "컨테이너"
    schedule_loaded = f"{scheduler_time()} 내일"
    schedule_label = "스케줄러"
    schedule_actions_html = "<span class='status-pill'>NAS 자동 실행 사용 중</span>"
    if credential_source == "environment":
        login_actions_html = "<span class='status-pill'>환경변수 로그인 사용 중</span>"
    else:
        login_actions_html = "<span class='status-pill warning'>.env 로그인 필요</span>"
    body = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>쿠팡 쿠폰 자동화</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: system-ui, "Segoe UI", sans-serif;
      background: #f4f6f8;
      color: #1d2733;
    }}
    body {{ margin: 0; padding: 28px; }}
    main {{ max-width: 1120px; margin: 0 auto; }}
    h1 {{ margin: 0; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 0; font-size: 22px; letter-spacing: 0; }}
    .sub {{ margin: 8px 0 0; color: #5d6978; }}
    .topbar {{ display: flex; align-items: end; justify-content: space-between; gap: 18px; margin-bottom: 22px; }}
    .top-actions {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
    .inline-form {{ margin: 0; }}
    .stats {{ display: flex; gap: 10px; margin: 18px 0; flex-wrap: wrap; }}
    .stat {{ background: #fff; border: 1px solid #d9dee5; border-radius: 8px; padding: 12px 14px; min-width: 140px; }}
    .stat strong {{ display: block; font-size: 20px; }}
    .stat span {{ color: #667487; font-size: 13px; }}
    .panel {{ background: #fff; border: 1px solid #d9dee5; border-radius: 8px; overflow: hidden; margin-bottom: 18px; }}
    .panel-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 18px; border-bottom: 1px solid #e5e9ef; }}
    .run-actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .same-day-run {{
      display: flex; align-items: center; justify-content: space-between; gap: 16px;
      padding: 14px 18px; border-bottom: 1px solid #e5e9ef; background: #fbfcfe; flex-wrap: wrap;
    }}
    .same-day-title strong {{ display: block; font-size: 15px; }}
    .same-day-title span {{ display: block; margin-top: 4px; color: #667487; font-size: 13px; }}
    .same-day-controls {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    .same-day-controls label {{ margin: 0; font-size: 13px; }}
    .same-day-controls input[type="time"] {{ width: 124px; padding: 10px 12px; }}
    .today-test {{ background: #e9eef5; color: #26384d; }}
    .today-submit {{ background: #0b6bcb; color: #fff; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 14px 16px; border-bottom: 1px solid #eef1f5; text-align: left; vertical-align: middle; }}
    th {{ color: #667487; font-size: 13px; font-weight: 800; background: #fbfcfe; }}
    tr:last-child td {{ border-bottom: 0; }}
    input[type="checkbox"] {{ width: 18px; height: 18px; }}
    .check-cell {{ width: 44px; }}
    .name {{ font-weight: 800; margin-bottom: 6px; }}
    .muted {{ color: #667487; font-size: 13px; line-height: 1.4; }}
    .meta {{ display: flex; gap: 6px; flex-wrap: wrap; }}
    .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 8px; font-size: 12px; font-weight: 800; background: #eef1f5; color: #405064; }}
    .badge.download {{ background: #eaf4ff; color: #1163a6; }}
    .badge.instant {{ background: #fff2de; color: #9a5412; }}
    .badge.price {{ background: #eef6e9; color: #2f6d2f; }}
    .badge.rate {{ background: #f1edff; color: #5b3fb4; }}
    .num, .date {{ white-space: nowrap; color: #405064; }}
    .actions-cell {{ width: 132px; white-space: nowrap; }}
    .empty {{ text-align: center; padding: 56px 16px; color: #667487; }}
    button {{
      border: 0; border-radius: 7px; padding: 12px 16px; font-size: 15px; font-weight: 800;
      cursor: pointer; font-family: inherit;
    }}
    .primary, .submit {{ background: #346aff; color: #fff; }}
    .secondary {{ background: #1f8a70; color: #fff; }}
    .test {{ background: #26384d; color: #fff; }}
    .session {{ background: #eef1f5; color: #26384d; }}
    .status-pill {{ display: inline-flex; align-items: center; min-height: 24px; border-radius: 999px; padding: 9px 12px; background: #eef4ff; color: #174ea6; font-size: 13px; font-weight: 800; }}
    .status-pill.warning {{ background: #fff8d8; color: #8a5b00; }}
    .save {{ background: #e7ebf0; color: #1d2733; }}
    .edit {{ background: #eef4ff; color: #174ea6; padding: 9px 11px; margin-right: 6px; }}
    .delete {{ background: #fff0ee; color: #bf2a17; padding: 9px 11px; }}
    .notice {{ margin: 0 0 16px; padding: 12px 14px; background: #fff8d8; border: 1px solid #ead47a; border-radius: 8px; }}
    .notice.success {{ background: #eaf8f1; border-color: #9fd8bd; color: #176348; }}
    .notice.danger {{ background: #fff0ee; border-color: #f0b2a8; color: #8f2014; }}
    .execution-results {{ margin: 0 0 18px; background: #fff; border: 1px solid #d9dee5; border-radius: 8px; overflow: hidden; }}
    .result-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 18px; border-bottom: 1px solid #e5e9ef; }}
    .result-summary, .result-status {{ display: inline-flex; align-items: center; border-radius: 999px; font-size: 13px; font-weight: 800; white-space: nowrap; }}
    .result-summary {{ padding: 8px 11px; }}
    .result-status {{ padding: 5px 8px; }}
    .result-summary.success, .result-status.success {{ background: #eaf8f1; color: #176348; }}
    .result-summary.failed, .result-status.failed {{ background: #fff0ee; color: #a02b1a; }}
    .result-status.skipped {{ background: #eef1f5; color: #405064; }}
    .result-detail {{ min-width: 240px; color: #405064; line-height: 1.45; }}
    .result-table-wrap {{ overflow-x: auto; }}
    .operation-status {{ padding: 0; }}
    .status-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; padding: 16px 18px; }}
    .status-card {{ border: 1px solid #d9dee5; border-radius: 8px; padding: 14px; background: #fbfcfe; }}
    .status-card.success {{ border-color: #9fd8bd; background: #f5fcf8; }}
    .status-card.failed {{ border-color: #f0b2a8; background: #fff8f6; }}
    .status-card.pending {{ border-color: #ead47a; background: #fffdf0; }}
    .status-card-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-bottom: 10px; }}
    .status-card-head span {{ border-radius: 999px; padding: 5px 8px; background: rgba(255,255,255,.72); font-size: 12px; font-weight: 800; }}
    .status-metrics {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }}
    .status-metrics span {{ border-radius: 999px; padding: 5px 8px; background: #fff; border: 1px solid #e5e9ef; font-size: 12px; font-weight: 800; }}
    .status-foot {{ padding: 0 18px 16px; }}
    button:disabled {{ opacity: .52; cursor: not-allowed; }}
    @media (max-width: 640px) {{
      .execution-results table, .execution-results tbody, .execution-results tr, .execution-results td {{ display: block; width: 100%; box-sizing: border-box; }}
      .execution-results thead {{ display: none; }}
      .execution-results tr {{ padding: 12px 16px; border-bottom: 1px solid #eef1f5; }}
      .execution-results tr:last-child {{ border-bottom: 0; }}
      .execution-results td {{ display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 10px; padding: 6px 0; border: 0; white-space: normal; }}
      .execution-results td::before {{ content: attr(data-label); color: #667487; font-size: 12px; font-weight: 800; }}
      .execution-results .result-detail {{ min-width: 0; overflow-wrap: anywhere; }}
    }}
    pre {{ margin-top: 20px; background: #111827; color: #d1e7ff; padding: 16px; border-radius: 8px; overflow: auto; max-height: 420px; }}
    dialog {{ width: min(860px, calc(100vw - 32px)); border: 0; border-radius: 10px; padding: 0; box-shadow: 0 20px 60px rgba(15, 23, 42, .28); }}
    dialog::backdrop {{ background: rgba(15, 23, 42, .45); }}
    #coupon-form {{ padding: 24px; }}
    .modal-head {{ display: flex; align-items: start; justify-content: space-between; gap: 16px; margin-bottom: 20px; }}
    .modal-head p {{ margin: 6px 0 0; color: #667487; }}
    .icon-button {{ width: 38px; height: 38px; padding: 0; background: #eef1f5; color: #405064; font-size: 24px; line-height: 1; }}
    fieldset {{ border: 0; margin: 0 0 20px; padding: 0; }}
    legend {{ font-weight: 800; margin: 0 0 10px; }}
    label {{ display: block; font-weight: 700; margin: 18px 0 8px; }}
    input:not([type="radio"]):not([type="checkbox"]):not([type="time"]), textarea, select {{
      width: 100%; box-sizing: border-box; border: 1px solid #c7ced8; border-radius: 6px;
      padding: 12px; font-size: 16px; font-family: inherit; background: #fff;
    }}
    textarea {{ min-height: 150px; resize: vertical; line-height: 1.45; }}
    input[type="radio"] {{ width: auto; margin: 0; }}
    .choice-group {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    .choice {{
      display: flex; align-items: center; gap: 8px; margin: 0; padding: 12px 14px;
      border: 1px solid #c7ced8; border-radius: 8px; background: #fff; cursor: pointer;
    }}
    .choice:has(input:checked) {{ border-color: #346aff; background: #eef4ff; color: #174ea6; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 16px; }}
    [hidden] {{ display: none !important; }}
    .hint {{ color: #667487; font-size: 13px; margin-top: 6px; }}
    .summary {{ margin: 20px 0; padding: 14px 16px; background: #eef4ff; border: 1px solid #c8dcff; border-radius: 8px; }}
    .modal-actions {{ display: flex; gap: 10px; justify-content: flex-end; margin-top: 22px; }}
  </style>
</head>
<body>
  <main>
    <div class="topbar">
      <div>
        <h1>쿠팡 쿠폰 자동화</h1>
        <p class="sub">수동 실행과 NAS 자동 실행 모두 내일({target_date:%Y-%m-%d}) 쿠폰을 미리 생성합니다. 자동 실행은 매일 {scheduler_time()}에 다음날 00:00~23:59 쿠폰을 만듭니다.</p>
      </div>
      <div class="top-actions">
        {login_actions_html}
        {schedule_actions_html}
        <button class="secondary" type="button" data-open-product-modal>상품 추가</button>
        <button class="primary" type="button" data-open-modal>쿠폰 추가</button>
      </div>
    </div>

    <div class="stats">
      <div class="stat"><strong>{product_count}</strong><span>등록된 상품</span></div>
      <div class="stat"><strong>{coupon_count}</strong><span>등록된 쿠폰</span></div>
      <div class="stat"><strong>{option_total}</strong><span>전체 옵션 ID</span></div>
      <div class="stat"><strong>{coupon_count}</strong><span>전체 선택 시 생성 수</span></div>
      <div class="stat"><strong>{target_date:%m/%d}</strong><span>생성 대상일</span></div>
      <div class="stat"><strong>{credential_status}</strong><span>로그인 정보</span></div>
      <div class="stat"><strong>{schedule_status}</strong><span>자동 실행</span></div>
      <div class="stat"><strong>{schedule_loaded}</strong><span>{schedule_label}</span></div>
    </div>

    {message_html}
    {results_html}
    {operation_status}

    <form method="post" class="panel">
      <div class="panel-head">
        <strong>상품 목록</strong>
        <button class="secondary" type="button" data-open-product-modal>상품 추가</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>상품명</th>
            <th>옵션</th>
            <th>등록일</th>
            <th>관리</th>
          </tr>
        </thead>
        <tbody>
          {product_rows}
        </tbody>
      </table>
    </form>

    <form method="post" id="coupon-list-form" class="panel">
      <div class="panel-head">
        <strong>자동화 쿠폰 목록</strong>
        <div class="run-actions">
          <button class="test" type="submit" name="action" value="test">선택 입력 테스트</button>
          <button class="submit" type="submit" name="action" value="submit" onclick="return confirm('선택한 쿠폰을 실제로 발급할까요?')">선택 쿠폰 만들기 실행</button>
        </div>
      </div>
      <div class="same-day-run">
        <div class="same-day-title">
          <strong>당일 쿠폰 생성</strong>
          <span>선택한 쿠폰을 입력 시간부터 오늘 23:59까지 생성</span>
        </div>
        <div class="same-day-controls">
          <label for="same-day-start-time">시작 시간</label>
          <input id="same-day-start-time" type="time" name="same_day_start_time" value="{esc(same_day_start_time)}" step="60">
          <button class="today-test" type="submit" name="action" value="test_today">당일 입력 테스트</button>
          <button class="today-submit" type="submit" name="action" value="submit_today" onclick="return confirm('선택한 쿠폰을 입력한 시간부터 오늘 23:59까지 실제 발급할까요?')">당일 쿠폰 만들기</button>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th class="check-cell"><input type="checkbox" id="select-all" aria-label="전체 선택"></th>
            <th>쿠폰명</th>
            <th>상품</th>
            <th>조건</th>
            <th>수동 생성일</th>
            <th>관리</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </form>

    {modal_html}
    {product_modal}
    {output_html}
  </main>

  <script>
    var modal = document.getElementById('coupon-modal');
    var productModal = document.getElementById('product-modal');
    var openButtons = document.querySelectorAll('[data-open-modal]');
    var openProductButtons = document.querySelectorAll('[data-open-product-modal]');
    var closeButtons = document.querySelectorAll('[data-close-modal]');
    var closeProductButtons = document.querySelectorAll('[data-close-product-modal]');
    openButtons.forEach(function (button) {{
      button.addEventListener('click', function () {{
        if (modal.showModal) modal.showModal();
        else modal.setAttribute('open', 'open');
      }});
    }});
    closeButtons.forEach(function (button) {{
      button.addEventListener('click', function () {{
        if (modal.close) modal.close();
        else modal.removeAttribute('open');
      }});
    }});
    openProductButtons.forEach(function (button) {{
      button.addEventListener('click', function () {{
        if (productModal.showModal) productModal.showModal();
        else productModal.setAttribute('open', 'open');
      }});
    }});
    closeProductButtons.forEach(function (button) {{
      button.addEventListener('click', function () {{
        if (productModal.close) productModal.close();
        else productModal.removeAttribute('open');
      }});
    }});

    var selectAll = document.getElementById('select-all');
    var itemChecks = Array.prototype.slice.call(document.querySelectorAll('input[name="selected_ids"]'));
    if (selectAll) {{
      selectAll.addEventListener('change', function () {{
        itemChecks.forEach(function (checkbox) {{ checkbox.checked = selectAll.checked; }});
      }});
    }}
    itemChecks.forEach(function (checkbox) {{
      checkbox.addEventListener('change', function () {{
        selectAll.checked = itemChecks.length > 0 && itemChecks.every(function (item) {{ return item.checked; }});
      }});
    }});

    function radioValue(name) {{
      return document.querySelector('#coupon-form input[name="' + name + '"]:checked').value;
    }}

    function numericValue(id, fallback) {{
      var value = document.getElementById(id).value.trim();
      return value || fallback;
    }}

    function formatNumber(value) {{
      return /^\\d+$/.test(value) ? Number(value).toLocaleString('ko-KR') : value;
    }}

    function updateCouponForm() {{
      var kind = radioValue('coupon_kind');
      var type = radioValue('discount_type');
      var isDownload = kind === 'downloadable';
      var isRate = type === 'RATE';

      document.querySelectorAll('.download-only').forEach(function (element) {{
        element.hidden = !isDownload;
      }});
      document.querySelectorAll('.rate-only').forEach(function (element) {{
        element.hidden = !isRate;
      }});

      document.getElementById('discount_label').textContent = isRate ? '할인율(%)' : '할인금액';
      document.getElementById('discount_hint').textContent = isRate
        ? '예: 10 = 10% 할인'
        : '예: 2000 = 2,000원 할인';

      var minOrder = numericValue('min_order_price', '0');
      var discount = numericValue('discount', '0');
      var maxDiscount = numericValue('max_discount_price', '');
      var maxIssue = numericValue('max_issue_count', '0');
      var summary = isDownload ? '다운로드쿠폰 / ' : '즉시할인쿠폰 / ';
      summary += isRate ? '정률 / ' : '정액 / ';

      if (isRate) {{
        summary += isDownload
          ? formatNumber(minOrder) + '원 이상 구매 시 ' + discount + '% 할인'
          : discount + '% 할인';
        if (maxDiscount) {{
          summary += ', 최대 ' + formatNumber(maxDiscount) + '원';
        }}
      }} else {{
        summary += isDownload
          ? formatNumber(minOrder) + '원 이상 구매 시 ' + formatNumber(discount) + '원 할인'
          : formatNumber(discount) + '원 할인';
      }}
      if (isDownload) {{
        summary += ' / 최대 발급 ' + formatNumber(maxIssue) + '개';
      }}
      document.getElementById('summary').textContent = summary;
    }}

    document.querySelectorAll('#coupon-form input, #coupon-form textarea').forEach(function (element) {{
      element.addEventListener('input', updateCouponForm);
      element.addEventListener('change', updateCouponForm);
    }});
    updateCouponForm();
  </script>
</body>
</html>"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def require_auth(self, *, include_body: bool = True) -> bool:
        if not web_auth_enabled():
            return True
        if valid_basic_auth(self.headers.get("Authorization")):
            return True
        body = "Authentication required.".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Coupang Coupon"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body) if include_body else 0))
        self.end_headers()
        if include_body:
            self.wfile.write(body)
        return False

    def do_HEAD(self) -> None:
        if not self.require_auth(include_body=False):
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self.require_auth():
            return
        coupons, products = load_state()
        self.respond(page_html(coupons, products))

    def do_POST(self) -> None:
        if not self.require_auth():
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        parsed = urllib.parse.parse_qs(data)
        coupons, products = load_state()
        action = parsed.get("action", [""])[0]

        if "edit_product_id" in parsed:
            edit_id = parsed["edit_product_id"][0]
            product = next((item for item in products if item["id"] == edit_id), None)
            if not product:
                self.respond(page_html(coupons, products, "수정할 상품을 찾을 수 없습니다."), status=404)
                return
            self.respond(
                page_html(
                    coupons,
                    products,
                    modal_product=product,
                    show_product_modal=True,
                    modal_product_edit_id=edit_id,
                )
            )
            return

        if "edit_coupon_id" in parsed:
            edit_id = parsed["edit_coupon_id"][0]
            coupon = next((item for item in coupons if item["id"] == edit_id), None)
            if not coupon:
                self.respond(page_html(coupons, products, "수정할 쿠폰을 찾을 수 없습니다."), status=404)
                return
            self.respond(
                page_html(
                    coupons,
                    products,
                    modal_coupon=coupon,
                    show_modal=True,
                    modal_coupon_edit_id=edit_id,
                )
            )
            return

        if "delete_product_id" in parsed:
            delete_id = parsed["delete_product_id"][0]
            if any(coupon.get("product_id") == delete_id for coupon in coupons):
                self.respond(page_html(coupons, products, "이 상품을 사용하는 쿠폰이 있어 삭제할 수 없습니다."), status=400)
                return
            products = [product for product in products if product["id"] != delete_id]
            save_products(products)
            self.respond(page_html(coupons, products, "상품을 삭제했습니다."))
            return

        if "delete_coupon_id" in parsed:
            delete_id = parsed["delete_coupon_id"][0]
            coupons = [coupon for coupon in coupons if coupon["id"] != delete_id]
            save_coupons(coupons)
            clear_run_status_for_coupon_ids([delete_id])
            self.respond(page_html(coupons, products, "삭제했습니다."))
            return

        if action == "add_product":
            product = normalize_product({key: values[0] for key, values in parsed.items()})
            errors = validate_product(product)
            if errors:
                self.respond(
                    page_html(
                        coupons,
                        products,
                        " ".join(errors),
                        modal_product=product,
                        show_product_modal=True,
                    ),
                    status=400,
                )
                return
            products.append(product)
            save_products(products)
            self.respond(page_html(coupons, products, "상품을 추가했습니다."))
            return

        if action == "update_product":
            edit_id = parsed.get("product_id", [""])[0]
            existing = next((item for item in products if item["id"] == edit_id), None)
            if not existing:
                self.respond(page_html(coupons, products, "수정할 상품을 찾을 수 없습니다."), status=404)
                return
            raw_product = {key: values[0] for key, values in parsed.items()}
            raw_product["id"] = edit_id
            raw_product["created_at"] = existing["created_at"]
            product = normalize_product(raw_product)
            errors = validate_product(product)
            if errors:
                self.respond(
                    page_html(
                        coupons,
                        products,
                        " ".join(errors),
                        modal_product=product,
                        show_product_modal=True,
                        modal_product_edit_id=edit_id,
                    ),
                    status=400,
                )
                return

            products = [product if item["id"] == edit_id else item for item in products]
            affected_coupon_ids: list[str] = []
            for coupon in coupons:
                if coupon.get("product_id") == edit_id:
                    coupon["vendor_item_ids"] = product["vendor_item_ids"]
                    affected_coupon_ids.append(coupon["id"])
            save_products(products)
            if affected_coupon_ids:
                save_coupons(coupons)
                reset_count = clear_run_status_for_coupon_ids(affected_coupon_ids)
                message = f"상품을 수정했습니다. 연결된 쿠폰 {len(affected_coupon_ids)}개의 오늘 이후 실행 기록 {reset_count}건을 초기화했습니다."
            else:
                message = "상품을 수정했습니다."
            self.respond(page_html(coupons, products, message))
            return

        if action == "add_coupon":
            coupon = normalize_coupon({key: values[0] for key, values in parsed.items()})
            product = product_by_id(products).get(coupon.get("product_id", ""))
            if product:
                coupon["vendor_item_ids"] = product["vendor_item_ids"]
            errors = validate_coupon(coupon)
            if errors:
                self.respond(
                    page_html(coupons, products, " ".join(errors), modal_coupon=coupon, show_modal=True),
                    status=400,
                )
                return
            coupons.append(coupon)
            save_coupons(coupons)
            self.respond(page_html(coupons, products, "쿠폰을 추가했습니다."))
            return

        if action == "update_coupon":
            edit_id = parsed.get("coupon_id", [""])[0]
            existing = next((item for item in coupons if item["id"] == edit_id), None)
            if not existing:
                self.respond(page_html(coupons, products, "수정할 쿠폰을 찾을 수 없습니다."), status=404)
                return
            raw_coupon = {key: values[0] for key, values in parsed.items()}
            raw_coupon["id"] = edit_id
            raw_coupon["created_at"] = existing["created_at"]
            coupon = normalize_coupon(raw_coupon)
            product = product_by_id(products).get(coupon.get("product_id", ""))
            if product:
                coupon["vendor_item_ids"] = product["vendor_item_ids"]
            errors = validate_coupon(coupon)
            if errors:
                self.respond(
                    page_html(
                        coupons,
                        products,
                        " ".join(errors),
                        modal_coupon=coupon,
                        show_modal=True,
                        modal_coupon_edit_id=edit_id,
                    ),
                    status=400,
                )
                return
            coupons = [coupon if item["id"] == edit_id else item for item in coupons]
            save_coupons(coupons)
            reset_count = clear_run_status_for_coupon_ids([edit_id])
            self.respond(page_html(coupons, products, f"쿠폰을 수정했습니다. 오늘 이후 실행 기록 {reset_count}건을 초기화했습니다."))
            return

        if action == "recover_failed":
            target_date, recover_coupons = recoverable_coupons(coupons)
            if not target_date or not recover_coupons:
                self.respond(page_html(coupons, products, "복구할 실패/미완료 쿠폰이 없습니다."))
                return
            code, output, results = run_automation(
                recover_coupons,
                submit=True,
                target_date=target_date,
            )
            failed_count = sum(1 for result in results if result.get("status") == "failed")
            success_count = sum(1 for result in results if result.get("status") == "success")
            skipped_count = sum(1 for result in results if result.get("status") == "skipped")
            if failed_count == 0:
                message = f"복구 실행 완료: {success_count}개 성공"
                if skipped_count:
                    message += f", {skipped_count}개 건너뜀"
                message += "했습니다."
            else:
                message = f"복구 실행 완료: {success_count}개 성공, {failed_count}개 실패했습니다."
            self.respond(
                page_html(
                    coupons,
                    products,
                    message,
                    output,
                    execution_results=results,
                    execution_submit=True,
                ),
                status=200 if code == 0 and failed_count == 0 else 500,
            )
            return

        if action in {"test", "submit", "test_today", "submit_today"}:
            selected = selected_coupons(coupons, parsed.get("selected_ids", []))
            if not selected:
                self.respond(page_html(coupons, products, "실행할 쿠폰을 하나 이상 선택하세요."), status=400)
                return
            target_date = None
            start_time = None
            if action in {"test_today", "submit_today"}:
                raw_start_time = parsed.get("same_day_start_time", [""])[0]
                start_time, error = validate_same_day_start_time(raw_start_time)
                if error:
                    self.respond(
                        page_html(
                            coupons,
                            products,
                            error,
                            same_day_start_time=raw_start_time,
                        ),
                        status=400,
                    )
                    return
                target_date = dt.datetime.now(KST).date()
            submit = action in {"submit", "submit_today"}
            code, output, results = run_automation(
                selected,
                submit=submit,
                target_date=target_date,
                start_time=start_time,
            )
            failed_count = sum(1 for result in results if result.get("status") == "failed")
            skipped_count = sum(1 for result in results if result.get("status") == "skipped")
            success_count = len(results) - failed_count - skipped_count
            action_label = "쿠폰 발급" if submit else "입력 테스트"
            if failed_count == 0:
                message = f"{action_label} 완료: {success_count}개 성공"
                if skipped_count:
                    message += f", {skipped_count}개 건너뜀"
                message += "했습니다."
            elif success_count:
                message = f"{action_label} 완료: {success_count}개 성공, {failed_count}개 실패했습니다."
            else:
                message = f"{action_label} 실패: 선택한 {failed_count}개 쿠폰을 만들지 못했습니다."
            self.respond(
                page_html(
                    coupons,
                    products,
                    message,
                    output,
                    same_day_start_time=start_time or "",
                    execution_results=results,
                    execution_submit=submit,
                ),
                status=200 if code == 0 and failed_count == 0 else 500,
            )
            return

        self.respond(page_html(coupons, products, "알 수 없는 요청입니다."), status=400)

    def respond(self, content: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("COUPON_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("COUPON_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{port}"
    print(f"쿠폰 자동화 웹폼: {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
