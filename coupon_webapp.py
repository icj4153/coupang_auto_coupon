#!/usr/bin/env python3
"""Local web UI for running the Coupang WING coupon automation."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import hmac
import html
import json
import os
import plistlib
import shlex
import subprocess
import sys
import threading
import urllib.parse
import uuid
import webbrowser
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
KST = ZoneInfo("Asia/Seoul")
OSASCRIPT = Path("/usr/bin/osascript")
LAUNCHCTL = Path("/bin/launchctl")
LAUNCH_AGENT_LABEL = "com.joon.coupang-coupon"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

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


def write_csv(coupons: list[dict[str, str]]) -> None:
    GENERATED_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with GENERATED_CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
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


def run_automation(coupons: list[dict[str, str]], submit: bool) -> tuple[int, str]:
    LOG_DIR.mkdir(exist_ok=True)
    write_csv(coupons)
    stamp = dt.datetime.now(KST).strftime("%Y-%m-%d_%H%M%S")
    log_path = LOG_DIR / f"{stamp}_{'submit' if submit else 'test'}.log"
    config_path = os.environ.get("COUPON_CONFIG_PATH", "browser_coupon_config.json")
    cmd = [
        sys.executable,
        str(ROOT / "wing_coupon_browser.py"),
        "--config",
        config_path,
        "--csv",
        str(GENERATED_CSV_PATH),
        "--days",
        "1",
        "--auto-login",
    ]
    if submit:
        cmd.append("--submit")
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    log_path.write_text(completed.stdout, encoding="utf-8")
    return completed.returncode, completed.stdout


def launch_login_setup_terminal() -> tuple[bool, str]:
    command = (
        f"cd {shlex.quote(str(ROOT))} && "
        "python3 wing_coupon_browser.py --config browser_coupon_config.json --setup-login; "
        "status=$?; "
        "echo; "
        "if [ $status -eq 0 ]; then "
        "echo '[wing-coupon] 로그인 세션 저장이 완료되었습니다.'; "
        "else "
        "echo '[wing-coupon] 로그인 세션 저장에 실패했습니다. 위 메시지를 확인하세요.'; "
        "fi; "
        "echo; "
        "read -r -p '확인 후 Enter를 누르면 이 터미널 창을 닫아도 됩니다. '"
    )
    script = [
        "on run argv",
        'tell application "Terminal"',
        "activate",
        "do script (item 1 of argv)",
        "end tell",
        "end run",
    ]
    try:
        completed = subprocess.run(
            [str(OSASCRIPT), *[arg for line in script for arg in ("-e", line)], command],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except FileNotFoundError:
        return False, "macOS Terminal 실행 도구(osascript)를 찾을 수 없습니다."
    except subprocess.TimeoutExpired:
        return False, "Terminal 실행 요청이 시간 초과되었습니다."

    if completed.returncode != 0:
        return False, completed.stdout.strip() or "Terminal 실행에 실패했습니다."
    return True, (
        "로그인 세션 만들기 Terminal 창을 열었습니다.\n"
        "열린 Chrome에서 쿠팡 WING에 로그인한 뒤, Terminal 안내에 따라 Enter를 누르면 세션이 저장됩니다."
    )


def save_keychain_credentials(login_id: str, password: str) -> tuple[bool, str]:
    try:
        wing_credentials.save_credentials(login_id, password)
    except wing_credentials.CredentialError as exc:
        return False, str(exc)
    return True, "로그인 정보를 macOS Keychain에 저장했습니다."


def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def schedule_is_installed() -> bool:
    return LAUNCH_AGENT_PATH.exists()


def schedule_is_loaded() -> bool:
    if not LAUNCHCTL.exists():
        return False
    completed = subprocess.run(
        [str(LAUNCHCTL), "print", f"{launchd_domain()}/{LAUNCH_AGENT_LABEL}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.returncode == 0


def launchd_plist() -> dict[str, object]:
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            "/usr/bin/env",
            "python3",
            str(ROOT / "daily_coupon_runner.py"),
            "--run",
        ],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": {"Hour": 0, "Minute": 1},
        "StandardOutPath": str(LOG_DIR / "daily_coupon_launchd.out.log"),
        "StandardErrorPath": str(LOG_DIR / "daily_coupon_launchd.err.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }


def install_daily_schedule() -> tuple[bool, str]:
    LOG_DIR.mkdir(exist_ok=True)
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LAUNCH_AGENT_PATH.open("wb") as handle:
        plistlib.dump(launchd_plist(), handle, sort_keys=False)

    if not LAUNCHCTL.exists():
        return False, "launchctl을 찾을 수 없습니다: /bin/launchctl"

    subprocess.run(
        [str(LAUNCHCTL), "bootout", launchd_domain(), str(LAUNCH_AGENT_PATH)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    completed = subprocess.run(
        [str(LAUNCHCTL), "bootstrap", launchd_domain(), str(LAUNCH_AGENT_PATH)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        return False, completed.stdout.strip() or "launchd 자동 실행 등록에 실패했습니다."

    subprocess.run(
        [str(LAUNCHCTL), "enable", f"{launchd_domain()}/{LAUNCH_AGENT_LABEL}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return True, "매일 00:01 자동 실행을 설치했습니다. Mac이 켜져 있고 로그인된 상태여야 실행됩니다."


def uninstall_daily_schedule() -> tuple[bool, str]:
    if LAUNCHCTL.exists() and LAUNCH_AGENT_PATH.exists():
        subprocess.run(
            [str(LAUNCHCTL), "bootout", launchd_domain(), str(LAUNCH_AGENT_PATH)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    if LAUNCH_AGENT_PATH.exists():
        LAUNCH_AGENT_PATH.unlink()
    return True, "매일 자동 실행을 해제했습니다."


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
                <button class="delete" type="submit" name="delete_coupon_id" value="{esc(coupon['id'])}" onclick="return confirm('이 쿠폰을 삭제할까요?')">삭제</button>
              </td>
            </tr>
            """
        )
    return "\n".join(rows)


def modal_form_html(coupon: dict[str, str], products: list[dict[str, str]], show_modal: bool) -> str:
    coupon = normalize_coupon(coupon)
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
    return f"""
    <dialog id="coupon-modal" {open_attr}>
      <form id="coupon-form" method="post">
        <input type="hidden" name="action" value="add_coupon">
        <div class="modal-head">
          <div>
            <h2>쿠폰 추가</h2>
            <p>저장 후 목록에서 선택해 실행합니다.</p>
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
          <button class="submit" type="submit">저장</button>
        </div>
      </form>
    </dialog>
    """


def product_modal_html(product: dict[str, str], show_modal: bool) -> str:
    product = normalize_product(product)
    open_attr = "open" if show_modal else ""
    return f"""
    <dialog id="product-modal" {open_attr}>
      <form id="product-form" method="post">
        <input type="hidden" name="action" value="add_product">
        <div class="modal-head">
          <div>
            <h2>상품 추가</h2>
            <p>옵션ID 묶음을 저장한 뒤 쿠폰에서 선택합니다.</p>
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
          <button class="submit" type="submit">저장</button>
        </div>
      </form>
    </dialog>
    """


def credential_modal_html(show_modal: bool) -> str:
    open_attr = "open" if show_modal else ""
    return f"""
    <dialog id="credential-modal" {open_attr}>
      <form id="credential-form" method="post">
        <input type="hidden" name="action" value="save_credentials">
        <div class="modal-head">
          <div>
            <h2>로그인 정보 저장</h2>
            <p>쿠팡 WING ID/PW는 파일이 아니라 macOS Keychain에 저장됩니다.</p>
          </div>
          <button class="icon-button" type="button" data-close-credential-modal aria-label="닫기">×</button>
        </div>

        <label for="login_id">WING 로그인 ID</label>
        <input id="login_id" name="login_id" autocomplete="username" required>

        <label for="login_password">WING 비밀번호</label>
        <input id="login_password" name="login_password" type="password" autocomplete="current-password" required>
        <div class="hint">자동 실행 중 추가 인증/보안 확인이 뜨면 해당 실행은 실패 로그를 남기고 멈춥니다.</div>

        <div class="modal-actions">
          <button class="save" type="button" data-close-credential-modal>취소</button>
          <button class="submit" type="submit">Keychain에 저장</button>
        </div>
      </form>
    </dialog>
    """


def page_html(
    coupons: list[dict[str, str]],
    products: list[dict[str, str]],
    message: str = "",
    output: str = "",
    *,
    modal_coupon: dict[str, str] | None = None,
    show_modal: bool = False,
    modal_product: dict[str, str] | None = None,
    show_product_modal: bool = False,
    show_credential_modal: bool = False,
) -> bytes:
    target_date = dt.datetime.now(KST).date() + dt.timedelta(days=1)
    message_html = f"<div class='notice'>{esc(message)}</div>" if message else ""
    output_html = f"<pre>{esc(output[-10000:])}</pre>" if output else ""
    coupon_count = len(coupons)
    product_count = len(products)
    option_total = sum(option_count(product) for product in products)
    modal_html = modal_form_html(modal_coupon or DEFAULT_COUPON, products, show_modal)
    product_modal = product_modal_html(modal_product or DEFAULT_PRODUCT, show_product_modal)
    credential_modal = credential_modal_html(show_credential_modal)
    product_rows = product_rows_html(products)
    rows_html = coupon_rows_html(coupons, products, target_date)
    credential_source = wing_credentials.credential_source()
    credential_status = {
        "environment": "환경변수",
        "keychain": "저장됨",
    }.get(credential_source, "미저장")
    schedule_status = "설치됨" if schedule_is_installed() else "미설치"
    schedule_loaded = "로드됨" if schedule_is_loaded() else "대기"
    body = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>쿠팡 쿠폰 자동화</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
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
    .actions-cell {{ width: 84px; }}
    .empty {{ text-align: center; padding: 56px 16px; color: #667487; }}
    button {{
      border: 0; border-radius: 7px; padding: 12px 16px; font-size: 15px; font-weight: 800;
      cursor: pointer; font-family: inherit;
    }}
    .primary, .submit {{ background: #346aff; color: #fff; }}
    .secondary {{ background: #1f8a70; color: #fff; }}
    .test {{ background: #26384d; color: #fff; }}
    .session {{ background: #eef1f5; color: #26384d; }}
    .save {{ background: #e7ebf0; color: #1d2733; }}
    .delete {{ background: #fff0ee; color: #bf2a17; padding: 9px 11px; }}
    .notice {{ margin: 0 0 16px; padding: 12px 14px; background: #fff8d8; border: 1px solid #ead47a; border-radius: 8px; }}
    .notice.danger {{ background: #fff0ee; border-color: #f0b2a8; color: #8f2014; }}
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
    input:not([type="radio"]):not([type="checkbox"]), textarea, select {{
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
        <p class="sub">저장된 쿠폰을 선택해서 내일({target_date:%Y-%m-%d}) 쿠폰만 생성합니다.</p>
      </div>
      <div class="top-actions">
        <button class="session" type="button" data-open-credential-modal>로그인 정보 저장</button>
        <form class="inline-form" method="post">
          <button class="session" type="submit" name="action" value="setup_login">수동 로그인</button>
        </form>
        <form class="inline-form" method="post">
          <button class="session" type="submit" name="action" value="install_schedule">매일 00:01 자동 실행 설치</button>
        </form>
        <form class="inline-form" method="post">
          <button class="delete" type="submit" name="action" value="uninstall_schedule">자동 실행 해제</button>
        </form>
        <button class="secondary" type="button" data-open-product-modal>상품 추가</button>
        <button class="primary" type="button" data-open-modal>쿠폰 추가</button>
      </div>
    </div>

    <div class="stats">
      <div class="stat"><strong>{product_count}</strong><span>등록된 상품</span></div>
      <div class="stat"><strong>{coupon_count}</strong><span>등록된 쿠폰</span></div>
      <div class="stat"><strong>{option_total}</strong><span>전체 옵션 ID</span></div>
      <div class="stat"><strong>{coupon_count}</strong><span>전체 선택 시 생성 수</span></div>
      <div class="stat"><strong>{target_date:%m/%d}</strong><span>생성일</span></div>
      <div class="stat"><strong>{credential_status}</strong><span>로그인 정보</span></div>
      <div class="stat"><strong>{schedule_status}</strong><span>자동 실행</span></div>
      <div class="stat"><strong>{schedule_loaded}</strong><span>launchd 상태</span></div>
    </div>

    {message_html}

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
      <table>
        <thead>
          <tr>
            <th class="check-cell"><input type="checkbox" id="select-all" aria-label="전체 선택"></th>
            <th>쿠폰명</th>
            <th>상품</th>
            <th>조건</th>
            <th>생성일</th>
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
    {credential_modal}
    {output_html}
  </main>

  <script>
    var modal = document.getElementById('coupon-modal');
    var productModal = document.getElementById('product-modal');
    var credentialModal = document.getElementById('credential-modal');
    var openButtons = document.querySelectorAll('[data-open-modal]');
    var openProductButtons = document.querySelectorAll('[data-open-product-modal]');
    var openCredentialButtons = document.querySelectorAll('[data-open-credential-modal]');
    var closeButtons = document.querySelectorAll('[data-close-modal]');
    var closeProductButtons = document.querySelectorAll('[data-close-product-modal]');
    var closeCredentialButtons = document.querySelectorAll('[data-close-credential-modal]');
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
    openCredentialButtons.forEach(function (button) {{
      button.addEventListener('click', function () {{
        if (credentialModal.showModal) credentialModal.showModal();
        else credentialModal.setAttribute('open', 'open');
      }});
    }});
    closeCredentialButtons.forEach(function (button) {{
      button.addEventListener('click', function () {{
        if (credentialModal.close) credentialModal.close();
        else credentialModal.removeAttribute('open');
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

        if action == "setup_login":
            ok, output = launch_login_setup_terminal()
            message = "로그인 세션 만들기를 시작했습니다." if ok else "로그인 세션 만들기 실행에 실패했습니다."
            self.respond(page_html(coupons, products, message, output), status=200 if ok else 500)
            return

        if action == "save_credentials":
            login_id = parsed.get("login_id", [""])[0]
            password = parsed.get("login_password", [""])[0]
            ok, output = save_keychain_credentials(login_id, password)
            message = "로그인 정보를 저장했습니다." if ok else "로그인 정보 저장에 실패했습니다."
            self.respond(
                page_html(coupons, products, message, output, show_credential_modal=not ok),
                status=200 if ok else 400,
            )
            return

        if action == "install_schedule":
            if not wing_credentials.has_credentials():
                self.respond(
                    page_html(
                        coupons,
                        products,
                        "자동 실행 전에 로그인 정보를 먼저 저장하세요.",
                        show_credential_modal=True,
                    ),
                    status=400,
                )
                return
            ok, output = install_daily_schedule()
            message = "자동 실행을 설치했습니다." if ok else "자동 실행 설치에 실패했습니다."
            self.respond(page_html(coupons, products, message, output), status=200 if ok else 500)
            return

        if action == "uninstall_schedule":
            ok, output = uninstall_daily_schedule()
            message = "자동 실행을 해제했습니다." if ok else "자동 실행 해제에 실패했습니다."
            self.respond(page_html(coupons, products, message, output), status=200 if ok else 500)
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

        if action in {"test", "submit"}:
            selected = selected_coupons(coupons, parsed.get("selected_ids", []))
            if not selected:
                self.respond(page_html(coupons, products, "실행할 쿠폰을 하나 이상 선택하세요."), status=400)
                return
            code, output = run_automation(selected, submit=(action == "submit"))
            message = "실행 성공" if code == 0 else f"실행 실패(exit={code}). 로그와 디버그 파일을 확인하세요."
            self.respond(page_html(coupons, products, message, output), status=200 if code == 0 else 500)
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
    if os.environ.get("COUPON_OPEN_BROWSER", "true").strip().lower() not in {"0", "false", "no", "off"}:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
