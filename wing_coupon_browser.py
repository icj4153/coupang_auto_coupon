#!/usr/bin/env python3
"""Playwright automation for Coupang WING coupon creation.

This script uses a persistent browser profile so the seller can log in once and
reuse that session for scheduled runs.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from playwright.sync_api import Browser, BrowserContext, Locator, Page, TimeoutError, sync_playwright

from wing_credentials import load_credentials


KST = ZoneInfo("Asia/Seoul")
ARTIFACT_DIR = Path(os.environ.get("COUPON_ARTIFACT_DIR", "browser_artifacts")).expanduser()
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
COUPON_MODAL_SELECTOR = "[role='dialog'].n-modal, .n-card.n-modal, .n-modal-container"
RESULTS_BEGIN = "__WING_COUPON_RESULTS_BEGIN__"
RESULTS_END = "__WING_COUPON_RESULTS_END__"


class AutomationError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[wing-coupon] {message}", flush=True)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log(f"Ignoring invalid {name}={raw!r}; using {default}.")
        return default
    return max(minimum, min(maximum, value))


def round_up_to_minute(value: dt.datetime) -> dt.datetime:
    if value.second or value.microsecond:
        value += dt.timedelta(minutes=1)
    return value.replace(second=0, microsecond=0)


def parse_clock_time(value: str) -> dt.time:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        raise AutomationError(f"Invalid start time: {value!r}. Use HH:MM.")
    return dt.time(int(match.group(1)), int(match.group(2)))


def parse_vendor_items(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\s,;]+", value) if part.strip()]


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader, start=2):
            if not truthy(row.get("enabled", "true")):
                continue
            rows.append(
                {
                    "line": index,
                    "coupon_id": row.get("coupon_id", "").strip(),
                    "campaign_name": row["campaign_name"].strip(),
                    "coupon_kind": row.get("coupon_kind", "instant").strip().lower(),
                    "vendor_item_ids": parse_vendor_items(row["vendor_item_ids"]),
                    "discount_type": row["discount_type"].strip().upper(),
                    "discount": row["discount"].strip(),
                    "min_order_price": row.get("min_order_price", "0").strip(),
                    "max_discount_price": row.get("max_discount_price", "").strip(),
                    "max_issue_count": row.get("max_issue_count", "").strip(),
                }
            )
    if not rows:
        raise AutomationError(f"No enabled rows found in {path}")
    return rows


def coupon_window(
    target_date: dt.date,
    now: dt.datetime | None = None,
    start_time: str | None = None,
) -> tuple[str, str]:
    now_kst = now.astimezone(KST) if now else dt.datetime.now(KST)
    start_at = dt.datetime.combine(target_date, dt.time(0, 0, 0))
    end_at = dt.datetime.combine(target_date, dt.time(23, 59, 0))

    if start_time:
        start_at = dt.datetime.combine(target_date, parse_clock_time(start_time))
        log(f"Using explicit coupon start time: {start_at:%Y-%m-%dT%H:%M}.")
    elif target_date == now_kst.date():
        buffer_minutes = env_int("COUPON_TODAY_START_BUFFER_MINUTES", 5, minimum=1, maximum=120)
        start_at = round_up_to_minute(now_kst.replace(tzinfo=None) + dt.timedelta(minutes=buffer_minutes))
        log(
            "Same-day coupon target detected. "
            f"Start time adjusted to {start_at:%Y-%m-%dT%H:%M} (now + {buffer_minutes}m)."
        )

    if start_at > end_at:
        raise AutomationError(
            f"Cannot create a coupon for {target_date}: start time {start_at:%H:%M} is after 23:59."
        )

    return (
        start_at.strftime("%Y-%m-%dT%H:%M"),
        end_at.strftime("%Y-%m-%dT%H:%M"),
    )


def campaign_name(base: str, target_date: dt.date, *, append_date_suffix: bool = False) -> str:
    base = base.strip()
    if append_date_suffix:
        return f"{base} {target_date.strftime('%m/%d')}"[:45]
    return base[:45]


def artifact_prefix(label: str) -> Path:
    ARTIFACT_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_")
    return ARTIFACT_DIR / f"{stamp}_{safe_label}"


def save_artifacts(page: Page, label: str) -> None:
    prefix = artifact_prefix(label)
    page.screenshot(path=f"{prefix}.png", full_page=True)
    prefix.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    print(f"Saved debug artifacts: {prefix}.png / {prefix}.html", file=sys.stderr)


def looks_like_login(page: Page) -> bool:
    url = page.url.lower()
    if "xauth.coupang.com" in url or "/login" in url:
        return True
    return False


def looks_like_access_denied(page: Page) -> bool:
    title = ""
    body_text = ""
    try:
        title = page.title().lower()
    except Exception:
        pass
    try:
        body_text = page.locator("body").inner_text(timeout=1000).lower()
    except Exception:
        pass
    return "access denied" in title or "access denied" in body_text


def wing_cookies(context: BrowserContext) -> list[dict[str, Any]]:
    return [
        cookie
        for cookie in context.cookies()
        if "coupang.com" in cookie.get("domain", "")
    ]


def visible(locator: Locator, timeout: int = 1200) -> bool:
    try:
        locator.first.wait_for(state="visible", timeout=timeout)
        return True
    except TimeoutError:
        return False


def modal_is_open(page: Page) -> bool:
    return visible(page.locator(COUPON_MODAL_SELECTOR).last, timeout=100)


def wait_for_modal(page: Page, timeout: int = 5000) -> bool:
    return visible(page.locator(COUPON_MODAL_SELECTOR).last, timeout=timeout)


def modal(page: Page) -> Locator:
    return page.locator(COUPON_MODAL_SELECTOR).last


def coupon_form_scope(page: Page) -> Locator:
    modal_form = page.locator(COUPON_MODAL_SELECTOR).filter(has_text="쿠폰종류").last
    if visible(modal_form, timeout=250):
        return modal_form
    container = page.locator(".coupon-apply-container").filter(has_text="쿠폰종류").last
    if visible(container, timeout=250):
        return container
    any_form = page.locator("body").filter(has_text="쿠폰종류").last
    if visible(any_form, timeout=250):
        return any_form
    return page.locator("body")


def coupon_form_is_present(page: Page) -> bool:
    try:
        coupon_form_scope(page).locator("input[type='radio'][name='couponType']").first.wait_for(
            state="attached",
            timeout=1500,
        )
        return True
    except TimeoutError:
        return visible(page.get_by_text("쿠폰종류", exact=True), timeout=800)


def wait_for_coupon_form(page: Page, timeout: int = 5000) -> bool:
    deadline = time.monotonic() + (timeout / 1000)
    while time.monotonic() < deadline:
        scope = coupon_form_scope(page)
        try:
            scope.locator("input[type='radio'][name='couponType']").first.wait_for(
                state="attached",
                timeout=300,
            )
            return True
        except TimeoutError:
            if visible(scope.get_by_text("쿠폰종류", exact=True), timeout=150):
                return True
        page.wait_for_timeout(150)
    return False


def safe_click(locator: Locator, page: Page, text: str) -> bool:
    was_modal_open = modal_is_open(page)
    try:
        locator.first.click(timeout=3000)
        return True
    except TimeoutError as exc:
        if not was_modal_open and modal_is_open(page):
            log(f"Click on '{text}' opened or hit a modal; treating it as successful.")
            return True
        log(f"Normal click timed out for '{text}', trying force click.")
        try:
            locator.first.click(timeout=1500, force=True)
            return True
        except TimeoutError:
            log(f"Force click also timed out for '{text}'.")
            return False
        except Exception as force_exc:
            log(f"Force click failed for '{text}': {force_exc}")
            return False
    except Exception as exc:
        log(f"Click failed for '{text}': {exc}")
        return False


def click_or_js(locator: Locator, label: str, *, timeout: int = 4000) -> None:
    target = locator.first
    target.wait_for(state="visible", timeout=timeout)
    target.scroll_into_view_if_needed(timeout=timeout)
    try:
        target.click(timeout=timeout)
    except TimeoutError:
        log(f"Normal click timed out for '{label}', dispatching DOM click.")
        target.evaluate("(el) => el.click()")


def fill_control(locator: Locator, value: str) -> None:
    locator.wait_for(state="visible", timeout=3000)
    locator.scroll_into_view_if_needed(timeout=3000)
    locator.evaluate(
        """(el, value) => {
            el.focus();
            el.value = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        value,
    )


def form_item(page: Page, label: str) -> Locator:
    return coupon_form_scope(page).locator(
        f"xpath=.//*[contains(normalize-space(), '{label}')]"
        "/ancestor::*[contains(@class, 'n-form-item')][1]"
    ).first


def fill_form_input(page: Page, label: str, value: str, index: int = 0) -> None:
    fill_control(form_item(page, label).locator("input:not([type='radio'])").nth(index), value)
    log(f"Filled form input: {label} #{index + 1}")


def fill_discount_inputs(page: Page, discount_type: str, values: list[str]) -> None:
    value_attr = {
        "RATE": "DISCOUNT_RATE",
        "PRICE": "FIXED_DISCOUNT",
        "FIXED_WITH_QUANTITY": "FIXED_QUANTITY_DISCOUNT",
    }.get(discount_type)
    if not value_attr:
        raise AutomationError(f"Unsupported discount type: {discount_type}")

    item = form_item(page, "할인방식")
    selected_row = item.locator(
        f"xpath=.//input[@name='discountType' and @value='{value_attr}']"
        "/ancestor::label[1]/ancestor::div[contains(@class, 'n-flex')][1]"
    ).first
    sibling_row = selected_row.locator("xpath=./following-sibling::div[contains(@class, 'n-flex')][1]").first
    input_candidates = [
        selected_row.locator("input.n-input__input-el:not([disabled])"),
        sibling_row.locator("input.n-input__input-el:not([disabled])"),
        item.locator("input.n-input__input-el:not([disabled])"),
    ]
    inputs = None
    for candidate in input_candidates:
        if candidate.count() >= len(values) and visible(candidate.first, timeout=700):
            inputs = candidate
            break
    if inputs is None:
        raise AutomationError(f"Could not find enabled discount inputs for {discount_type}.")

    for index, value in enumerate(values):
        fill_control(inputs.nth(index), value)
        log(f"Filled discount input {discount_type} #{index + 1}")


def fill_form_textarea(page: Page, label: str, value: str) -> None:
    fill_control(form_item(page, label).locator("textarea").first, value)
    log(f"Filled form textarea: {label}")


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in parts) + ")"


def product_row(page: Page, vendor_item_id: str) -> Locator:
    return coupon_form_scope(page).locator(
        "xpath=.//td[@data-col-key='vendorItemId' and "
        f"normalize-space()={xpath_literal(vendor_item_id)}]/ancestor::tr[1]"
    ).first


def product_row_is_selected(row: Locator) -> bool:
    try:
        return bool(
            row.evaluate(
                """(row) => {
                    const input = row.querySelector('input[type="checkbox"]');
                    if (input && input.checked) return true;
                    return Boolean(row.querySelector('[aria-checked="true"], .n-checkbox--checked'));
                }"""
            )
        )
    except Exception:
        return False


def verify_product_rows(page: Page, vendor_item_ids: list[str], *, timeout: int = 15000) -> None:
    expected_ids = list(dict.fromkeys(vendor_item_ids))
    if not expected_ids:
        raise AutomationError("No option IDs were provided.")

    deadline = time.monotonic() + (timeout / 1000)
    found: list[str] = []
    while time.monotonic() < deadline:
        found = []
        for vendor_item_id in expected_ids:
            if visible(product_row(page, vendor_item_id), timeout=250):
                found.append(vendor_item_id)
        if len(found) == len(expected_ids):
            break
        page.wait_for_timeout(350)

    missing = [vendor_item_id for vendor_item_id in expected_ids if vendor_item_id not in found]
    if missing:
        raise AutomationError(
            "Product search did not return every option ID. "
            f"Missing: {', '.join(missing)}"
        )

    unselected: list[str] = []
    for vendor_item_id in expected_ids:
        row = product_row(page, vendor_item_id)
        if product_row_is_selected(row):
            continue
        checkbox = row.locator(".n-checkbox, input[type='checkbox']").first
        if visible(checkbox, timeout=600):
            safe_click(checkbox, page, f"product checkbox {vendor_item_id}")
            page.wait_for_timeout(250)
        if not product_row_is_selected(row):
            unselected.append(vendor_item_id)

    if unselected:
        raise AutomationError(
            "Product rows were found but not selected. "
            f"Unselected: {', '.join(unselected)}"
        )

    log(f"Verified selected product rows: {len(expected_ids)}/{len(expected_ids)}")


def click_form_label(page: Page, label: str) -> bool:
    target = coupon_form_scope(page).locator(
        f"xpath=.//*[normalize-space()='{label}']/ancestor::label[1] | .//*[normalize-space()='{label}']"
    ).first
    if not visible(target, timeout=1200):
        return False
    return safe_click(target, page, label)


def set_form_radio(page: Page, name: str, value: str, label: str) -> None:
    scope = coupon_form_scope(page)
    result = scope.evaluate(
        """(root, args) => {
            const doc = root.ownerDocument;
            const visible = (el) => Boolean(el && (el.offsetParent || el.getClientRects().length));
            const roots = [
                root,
                ...Array.from(doc.querySelectorAll('.n-modal-container, [role="dialog"].n-modal, .n-card.n-modal')).reverse(),
                ...Array.from(doc.querySelectorAll('.coupon-apply-container')).reverse(),
                doc.body,
            ].filter(Boolean);

            for (const candidateRoot of roots) {
                const rootText = candidateRoot.innerText || '';
                if (!rootText.includes('쿠폰종류') && !candidateRoot.querySelector(`input[type="radio"][name="${args.name}"]`)) {
                    continue;
                }
                const inputs = Array.from(candidateRoot.querySelectorAll('input[type="radio"]'))
                    .filter((input) => input.name === args.name);
                let input = inputs.find((item) => item.value === args.value);
                let label = input ? input.closest('label') : null;
                if (!label) {
                    label = Array.from(candidateRoot.querySelectorAll('label'))
                        .find((item) => (item.innerText || '').replace(/\\s+/g, '').includes(args.label.replace(/\\s+/g, '')));
                    input = label ? label.querySelector(`input[type="radio"][name="${args.name}"]`) : input;
                }
                if (!input && !label) continue;
                const clickTarget = label || input;
                clickTarget.scrollIntoView({ block: 'center', inline: 'center' });
                clickTarget.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                clickTarget.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                clickTarget.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                if (input) {
                    input.checked = true;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                }
                return {
                    ok: Boolean(input || label),
                    checked: input ? input.checked : false,
                    value: input ? input.value : '',
                    text: clickTarget.innerText || '',
                    visible: visible(clickTarget),
                };
            }
            return { ok: false, checked: false, value: '', text: '', visible: false };
        }""",
        {"name": name, "value": value, "label": label},
    )
    page.wait_for_timeout(700)
    if not result.get("ok"):
        save_artifacts(page, f"radio_not_found_{name}_{value}")
        raise AutomationError(f"Could not find radio: {label}")
    input_locator = coupon_form_scope(page).locator(f"input[type='radio'][name='{name}'][value='{value}']").first
    if visible(input_locator, timeout=1200) and not input_locator.is_checked():
        input_locator.check(force=True, timeout=3000)
        page.wait_for_timeout(500)
    if visible(input_locator, timeout=800) and not input_locator.is_checked():
        save_artifacts(page, f"radio_not_selected_{name}_{value}")
        raise AutomationError(f"Could not select radio: {label}")
    log(f"Selected form radio: {label}")


def click_modal_button(page: Page, label: str, *, timeout: int = 2500) -> bool:
    button = coupon_form_scope(page).get_by_role("button", name=re.compile(re.escape(label), re.I)).first
    if not visible(button, timeout=timeout):
        button = coupon_form_scope(page).get_by_text(label, exact=True).first
        if not visible(button, timeout=800):
            return False
    return safe_click(button, page, label)


def click_final_create_coupon(page: Page) -> None:
    scope = coupon_form_scope(page)
    button = scope.locator(
        "xpath=.//button[normalize-space()='할인쿠폰 만들기' or .//*[normalize-space()='할인쿠폰 만들기']]"
    ).last
    if not visible(button, timeout=3000):
        save_artifacts(page, "final_create_button_not_found")
        raise AutomationError("Could not find the final '할인쿠폰 만들기' button in the coupon form.")
    success = page.get_by_text(re.compile("쿠폰.*(성공|완료)|성공적으로 생성|생성이 완료", re.I)).last

    def wait_for_submit_completion(timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if visible(success, timeout=250):
                log("WING reported coupon creation success.")
                return True
            if not visible(button, timeout=250):
                log("Final coupon form closed after submit.")
                return True
            page.wait_for_timeout(300)
        return False

    for attempt in range(1, 3):
        click_or_js(button, "final 할인쿠폰 만들기", timeout=5000)
        confirm = page.locator(COUPON_MODAL_SELECTOR).last.get_by_role(
            "button",
            name=re.compile(r"^(확인|예|OK|Confirm)$", re.I),
        )
        if visible(confirm, timeout=3000):
            click_or_js(confirm, "final confirmation", timeout=5000)
        if wait_for_submit_completion(12 if attempt == 1 else 30):
            return
        if attempt == 1:
            log("Final coupon form stayed open after submit. Retrying the final submit once.")

    error_text = ""
    try:
        error_text = scope.locator(
            ".n-form-item-feedback, .n-message, .n-alert, .n-notification, .n-modal"
        ).last.inner_text(timeout=1000)
    except Exception:
        pass
    if not error_text:
        try:
            error_text = scope.inner_text(timeout=1000)[-1000:]
        except Exception:
            error_text = "No visible WING error text was captured."
    raise AutomationError(
        "Final coupon submit did not complete; the creation form stayed open. "
        f"Last visible text: {error_text}"
    )


def unique_texts(texts: list[str]) -> list[str]:
    seen = set()
    unique = []
    for text in texts:
        if text and text not in seen:
            seen.add(text)
            unique.append(text)
    return unique


def create_button_texts(config: dict[str, Any]) -> list[str]:
    return unique_texts(
        [
            "할인쿠폰 만들기",
            "쿠폰 만들기",
            *config.get("create_button_texts", []),
        ]
    )


def create_button_locator(page: Page, config: dict[str, Any]) -> Locator:
    pattern = re.compile("|".join(re.escape(text) for text in create_button_texts(config)), re.I)
    return page.get_by_role("button", name=pattern).last


def create_button_is_visible(page: Page, config: dict[str, Any], timeout: int = 120) -> bool:
    return visible(create_button_locator(page, config), timeout=timeout)


def click_create_coupon_button(page: Page, config: dict[str, Any], *, timeout: int = 2000) -> bool:
    button = create_button_locator(page, config)
    if visible(button, timeout=timeout):
        log("Clicking create coupon button.")
        return safe_click(button, page, "할인쿠폰 만들기")
    return click_first_text(page, create_button_texts(config), timeout=500)


def close_intro_popup(page: Page, config: dict[str, Any], *, timeout: int = 2500) -> None:
    start = time.monotonic()
    deadline = start + (timeout / 1000)
    popup = page.locator(COUPON_MODAL_SELECTOR).filter(
        has_text="쿠폰으로 매출 성장"
    ).last
    while time.monotonic() < deadline:
        if visible(popup, timeout=120):
            break
        if modal_is_open(page):
            continue
        if time.monotonic() - start > 0.35 and create_button_is_visible(page, config, timeout=80):
            return
    else:
        return

    close_button = popup.get_by_role("button", name=re.compile("닫기|Close", re.I)).first
    if visible(close_button, timeout=300):
        safe_click(close_button, page, "intro popup close")
    else:
        x_button = popup.locator(".n-base-close, [aria-label='close'], [aria-label='Close']").first
        if visible(x_button, timeout=300):
            safe_click(x_button, page, "intro popup x")
        else:
            page.keyboard.press("Escape")
    try:
        popup.wait_for(state="hidden", timeout=1000)
    except TimeoutError:
        pass
    log("Closed intro popup.")


def set_coupon_type_in_modal(page: Page, kind: str) -> None:
    if kind == "downloadable":
        set_form_radio(page, "couponType", "DOWNLOAD", "다운로드쿠폰")
    else:
        set_form_radio(page, "couponType", "INSTANT", "즉시할인쿠폰")


def set_discount_type_in_modal(page: Page, discount_type: str) -> None:
    values = {
        "RATE": ("DISCOUNT_RATE", "정률"),
        "PRICE": ("FIXED_DISCOUNT", "정액"),
        "FIXED_WITH_QUANTITY": ("FIXED_QUANTITY_DISCOUNT", "수량별 정액"),
    }
    value, label = values.get(discount_type, (discount_type, discount_type))
    set_form_radio(page, "discountType", value, label)


def fill_wing_coupon_modal(
    page: Page,
    row: dict[str, Any],
    display_name: str,
    start_at: str,
    end_at: str,
) -> None:
    set_coupon_type_in_modal(page, row["coupon_kind"])
    if row["coupon_kind"] == "downloadable":
        coupon_form_scope(page).get_by_placeholder(re.compile("행사명", re.I)).first.wait_for(state="visible", timeout=5000)
        fill_form_input(page, "할인쿠폰명", display_name, 1)
    else:
        fill_form_input(page, "할인쿠폰명", display_name)
    fill_form_input(page, "쿠폰 유효기간", start_at, 0)
    fill_form_input(page, "쿠폰 유효기간", end_at, 1)

    set_discount_type_in_modal(page, row["discount_type"])
    if row["coupon_kind"] == "downloadable":
        if row["discount_type"] == "RATE":
            values = [row["min_order_price"], row["discount"]]
            if row["max_discount_price"]:
                values.append(row["max_discount_price"])
            fill_discount_inputs(page, row["discount_type"], values)
        elif row["discount_type"] == "PRICE":
            fill_discount_inputs(page, row["discount_type"], [row["min_order_price"], row["discount"]])
        else:
            fill_discount_inputs(page, row["discount_type"], [row["discount"]])
    else:
        if row["discount_type"] == "RATE":
            values = [row["discount"]]
            if row["max_discount_price"]:
                values.append(row["max_discount_price"])
            fill_discount_inputs(page, row["discount_type"], values)
        elif row["discount_type"] == "PRICE":
            fill_discount_inputs(page, row["discount_type"], [row["discount"]])
        else:
            fill_discount_inputs(page, row["discount_type"], [row["discount"]])

    if row["coupon_kind"] == "downloadable" and row["max_issue_count"]:
        fill_form_input(page, "최대 발급 개수", row["max_issue_count"], 0)

    fill_form_textarea(page, "옵션 ID로 추가", "\n".join(row["vendor_item_ids"]))
    page.wait_for_timeout(700)
    if click_modal_button(page, "상품 조회", timeout=2000):
        log("Clicked 상품 조회.")
        verify_product_rows(page, row["vendor_item_ids"])
    else:
        raise AutomationError("Could not click the product search button.")


def click_first_text(page: Page, texts: list[str], *, timeout: int = 2500) -> bool:
    for text in texts:
        log(f"Looking for text/button: {text}")
        candidates = [
            page.get_by_role("button", name=re.compile(re.escape(text), re.I)),
            page.get_by_role("link", name=re.compile(re.escape(text), re.I)),
            page.get_by_text(text, exact=True),
            page.get_by_text(re.compile(re.escape(text), re.I)),
        ]
        for locator in candidates:
            if visible(locator, timeout=timeout):
                log(f"Clicking: {text}")
                if safe_click(locator, page, text):
                    return True
        log(f"Not found: {text}")
    return False


def fill_selector(page: Page, selector: str, value: str) -> bool:
    if not selector:
        return False
    locator = page.locator(selector).first
    if not visible(locator):
        return False
    locator.fill(value)
    return True


def fill_near_label(page: Page, labels: list[str], value: str) -> bool:
    for label in labels:
        candidates = [
            page.get_by_label(re.compile(re.escape(label), re.I)),
            page.get_by_placeholder(re.compile(re.escape(label), re.I)),
            page.locator(
                f"xpath=//*[contains(normalize-space(), '{label}')]"
                "/following::input[1]"
            ),
            page.locator(
                f"xpath=//*[contains(normalize-space(), '{label}')]"
                "/following::textarea[1]"
            ),
        ]
        for locator in candidates:
            if visible(locator):
                locator.first.fill(value)
                return True
    return False


def set_field(
    page: Page,
    selectors: dict[str, str],
    key: str,
    labels: list[str],
    value: str,
    *,
    required: bool = True,
) -> None:
    if value == "":
        return
    if fill_selector(page, selectors.get(key, ""), value):
        log(f"Filled {key} with configured selector.")
        return
    if fill_near_label(page, labels, value):
        log(f"Filled {key} by label text.")
        return
    if required:
        raise AutomationError(f"Could not fill field '{key}'. Add a selector in browser_coupon_config.json.")


def choose_coupon_kind(page: Page, kind: str) -> None:
    texts = {
        "instant": ["즉시할인", "즉시 할인", "Instant"],
        "downloadable": ["다운로드", "다운로드 쿠폰", "Downloadable"],
    }.get(kind, [kind])
    if click_first_text(page, texts, timeout=1200):
        log(f"Selected coupon kind: {kind}")
    else:
        log(f"Could not confidently select coupon kind: {kind}. Continuing.")


def choose_discount_type(page: Page, discount_type: str) -> None:
    texts = {
        "PRICE": ["정액", "금액", "원 할인", "Fixed amount", "PRICE"],
        "RATE": ["정률", "비율", "%", "Rate", "RATE"],
        "FIXED_WITH_QUANTITY": ["수량별", "개당", "FIXED_WITH_QUANTITY"],
    }.get(discount_type, [discount_type])
    if click_first_text(page, texts, timeout=1200):
        log(f"Selected discount type: {discount_type}")
    else:
        log(f"Could not confidently select discount type: {discount_type}. Continuing.")


def navigate_to_coupon_page(page: Page, config: dict[str, Any]) -> None:
    coupon_page_url = config.get("coupon_page_url", "").strip()
    if coupon_page_url:
        log(f"Opening configured coupon page: {coupon_page_url}")
        page.goto(coupon_page_url, wait_until="domcontentloaded")
        close_intro_popup(page, config)
        log(f"Current page: {page.title()} / {page.url}")
        if looks_like_login(page):
            log("Current page is WING login.")
        return

    log("Opening WING home.")
    page.goto(config.get("wing_url", "https://wing.coupang.com"), wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    log(f"Current page: {page.title()} / {page.url}")
    clicked_any = False
    for text in config.get("navigation_texts", []):
        if click_first_text(page, [text], timeout=900):
            clicked_any = True
            page.wait_for_timeout(1200)
            log(f"After clicking '{text}': {page.title()} / {page.url}")
    if not clicked_any:
        log("No navigation menu text was found. Saving page artifacts.")
        save_artifacts(page, "navigation_not_found")


def fill_login_field(page: Page, selectors: list[str], value: str, label: str) -> None:
    for selector in selectors:
        locator = page.locator(selector).first
        if visible(locator, timeout=1200):
            fill_control(locator, value)
            log(f"Filled login {label}.")
            return
    save_artifacts(page, f"login_{label.lower()}_field_not_found")
    raise AutomationError(f"Could not find WING login {label} field.")


def attempt_auto_login(page: Page, context: BrowserContext, config: dict[str, Any]) -> bool:
    login_id, password = load_credentials()
    if not login_id or not password:
        log("No WING login credentials found in environment variables.")
        return False

    if not looks_like_login(page):
        page.goto(config.get("wing_url", "https://wing.coupang.com"), wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        if not looks_like_login(page):
            return True

    log("Login session is missing or expired. Trying credential auto-login.")
    if looks_like_access_denied(page):
        save_artifacts(page, "wing_login_access_denied")
        raise AutomationError(
            "WING login returned Access Denied before showing the ID/PW form. "
            "Coupang may be blocking this NAS/headless browser session. "
            "Check the saved browser artifact in browser_artifacts."
        )
    fill_login_field(page, ["#username", "input[name='username']", "input[type='text']"], login_id, "ID")
    fill_login_field(page, ["#password", "input[name='password']", "input[type='password']"], password, "password")
    login_button = page.locator("#kc-login, input[name='login'], input[type='submit']").first
    if not visible(login_button, timeout=2000):
        raise AutomationError("Could not find WING login submit button.")
    login_button.click(timeout=3000)

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=1000)
        except TimeoutError:
            pass
        if looks_like_access_denied(page):
            save_artifacts(page, "wing_login_access_denied_after_submit")
            raise AutomationError("WING login redirect returned Access Denied after submitting credentials.")
        if not looks_like_login(page):
            log(f"Auto-login completed at: {page.title()} / {page.url}")
            save_storage_state(context, config)
            return True

    save_artifacts(page, "auto_login_failed")
    raise AutomationError(
        "Auto-login did not complete. Coupang may be asking for additional verification. "
        "Open the saved browser artifact and check whether additional verification is required."
    )


def ensure_logged_in(page: Page, context: BrowserContext, config: dict[str, Any], *, auto_login: bool) -> None:
    if not looks_like_login(page):
        return
    if auto_login:
        attempts = env_int("COUPANG_LOGIN_ATTEMPTS", 5, minimum=1, maximum=5)
        retry_seconds = env_int("COUPANG_LOGIN_RETRY_SECONDS", 60, minimum=10, maximum=600)
        for attempt in range(1, attempts + 1):
            try:
                if attempt_auto_login(page, context, config):
                    return
                break
            except AutomationError as exc:
                if attempt >= attempts:
                    raise
                log(
                    f"Auto-login attempt {attempt}/{attempts} failed: {exc} "
                    f"Retrying in {retry_seconds * attempt} seconds."
                )
                page.wait_for_timeout(retry_seconds * attempt * 1000)
                navigate_to_coupon_page(page, config)
    raise AutomationError("Login session expired. Set COUPANG_WING_ID and COUPANG_WING_PASSWORD in .env.")


def save_storage_state(context: BrowserContext, config: dict[str, Any]) -> None:
    path = config.get("storage_state_path", "wing_storage_state.json")
    context.storage_state(path=path)
    log(f"Saved browser storage state: {Path(path).resolve()}")


def wait_for_manual_coupon_page(page: Page) -> None:
    log("열린 브라우저에서 쿠팡 WING 쿠폰 생성/관리 페이지까지 직접 이동하세요.")
    log("쿠폰 작성 화면이 보이는 상태에서 터미널 Enter를 누르면 자동 입력을 이어갑니다.")
    input()
    page.wait_for_load_state("domcontentloaded")
    log(f"Manual coupon page selected: {page.title()} / {page.url}")
    save_artifacts(page, "manual_coupon_page")


def inspect_session(page: Page, config: dict[str, Any]) -> None:
    page.goto(config.get("wing_url", "https://wing.coupang.com"), wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    log(f"Session page: {page.title()} / {page.url}")
    log(f"Coupang cookies in profile: {len(wing_cookies(page.context))}")
    if looks_like_login(page):
        log("This still looks like the login page. Check COUPANG_WING_ID and COUPANG_WING_PASSWORD in .env.")
    else:
        log("This does not look like the login page.")
    save_artifacts(page, "inspect_session")
    log("스크린샷을 저장했습니다. 브라우저를 확인한 뒤 터미널 Enter를 누르면 종료합니다.")
    input()


def create_coupon(
    page: Page,
    config: dict[str, Any],
    row: dict[str, Any],
    target_date: dt.date,
    *,
    submit: bool,
    open_create_button: bool = True,
    append_date_suffix: bool = False,
    start_time: str | None = None,
) -> dict[str, Any]:
    selectors = config.get("selectors", {})
    start_at, end_at = coupon_window(target_date, start_time=start_time)
    display_name = campaign_name(row["campaign_name"], target_date, append_date_suffix=append_date_suffix)

    log(f"Creating coupon for CSV line {row['line']}: {display_name}")
    if open_create_button:
        close_intro_popup(page, config, timeout=1200)
        if not click_create_coupon_button(page, config, timeout=1800):
            raise AutomationError("Could not find the create coupon button.")
        if wait_for_coupon_form(page, timeout=5000):
            fill_wing_coupon_modal(page, row, display_name, start_at, end_at)
        else:
            raise AutomationError("Coupon creation form was not detected after clicking the create button.")
    elif coupon_form_is_present(page):
        log("Coupon form is already open; filling current page.")
        fill_wing_coupon_modal(page, row, display_name, start_at, end_at)
    else:
        if not click_create_coupon_button(page, config, timeout=1800):
            raise AutomationError("Could not find the create coupon button.")
        if wait_for_coupon_form(page, timeout=5000):
            fill_wing_coupon_modal(page, row, display_name, start_at, end_at)
        else:
            raise AutomationError("Coupon creation form was not detected. Stopping before filling the wrong fields.")

    artifact_label = f"line_{row['line']}_{target_date:%Y%m%d}"
    save_artifacts(page, f"preview_{artifact_label}")

    if submit:
        log("Submit mode enabled. Clicking final 할인쿠폰 만들기 button.")
        click_final_create_coupon(page)
        save_artifacts(page, f"submitted_{artifact_label}")

    return {
        "coupon_id": row.get("coupon_id", ""),
        "line": row["line"],
        "campaign_name": display_name,
        "target_date": str(target_date),
        "start_at": start_at,
        "submit": submit,
        "vendor_item_count": len(row["vendor_item_ids"]),
        "status": "success" if submit else "preview",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automate Coupang WING coupon creation with Playwright.")
    parser.add_argument("--config", default="browser_coupon_config.json", help="Browser automation JSON config.")
    parser.add_argument("--csv", default="browser_coupons.csv", help="Coupon CSV file.")
    parser.add_argument("--target-date", help="Coupon date YYYY-MM-DD. Defaults to tomorrow in KST.")
    parser.add_argument("--start-time", help="Explicit coupon start time HH:MM for the target date.")
    parser.add_argument("--days", type=int, default=1, help="Create one coupon per day starting at target date.")
    parser.add_argument("--append-date-suffix", action="store_true", help="Append MM/DD to coupon names.")
    parser.add_argument("--inspect", action="store_true", help="Open WING, print URL/title, save screenshot, then wait.")
    parser.add_argument("--manual-coupon-page", action="store_true", help="Let the user navigate to the coupon page before automation.")
    parser.add_argument("--submit", action="store_true", help="Actually click the final submit button.")
    parser.add_argument(
        "--auto-login",
        action="store_true",
        help="Use WING credentials from environment variables when login is needed.",
    )
    parser.add_argument(
        "--fresh-login",
        action="store_true",
        help="Ignore saved storage state and log in from scratch. Requires --auto-login.",
    )
    parser.add_argument(
        "--allow-multi-day-submit",
        action="store_true",
        help="Allow submitting more than one future day. Disabled by default because WING may silently drop product links.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue with later coupons when one coupon fails, then exit non-zero if any failed.",
    )
    return parser.parse_args()


def browser_launch_options(config: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {
        "headless": bool(config.get("headless", False)),
        "slow_mo": int(config.get("slow_mo_ms", 0)),
    }
    launch_args = config.get("launch_args", ["--disable-blink-features=AutomationControlled"])
    if isinstance(launch_args, list):
        options["args"] = [str(arg) for arg in launch_args if str(arg).strip()]
    browser_channel = str(config.get("browser_channel") or os.environ.get("COUPON_BROWSER_CHANNEL", "")).strip()
    if browser_channel:
        options["channel"] = browser_channel
    return options


def selected_browser_channel(config: dict[str, Any]) -> str:
    return str(config.get("browser_channel") or os.environ.get("COUPON_BROWSER_CHANNEL", "")).strip()


def new_context_options(config: dict[str, Any], *, use_storage_state: bool) -> dict[str, Any]:
    options: dict[str, Any] = {"viewport": {"width": 1440, "height": 950}}
    locale = str(config.get("locale", "ko-KR")).strip()
    if locale:
        options["locale"] = locale
        options["extra_http_headers"] = {
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    timezone_id = str(config.get("timezone_id", "Asia/Seoul")).strip()
    if timezone_id:
        options["timezone_id"] = timezone_id
    user_agent = str(config.get("user_agent", DEFAULT_USER_AGENT)).strip()
    if user_agent:
        options["user_agent"] = user_agent
    storage_path = Path(config.get("storage_state_path", "wing_storage_state.json"))
    if use_storage_state and storage_path.exists():
        options["storage_state"] = str(storage_path)
        log(f"Using saved storage state: {storage_path.resolve()}")
    elif use_storage_state:
        log(f"No storage state found yet: {storage_path.resolve()}")
    return options


def install_context_patches(context: BrowserContext, config: dict[str, Any]) -> None:
    if not bool(config.get("stealth", True)):
        return
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        window.chrome = window.chrome || { runtime: {} };
        """
    )


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    csv_path = Path(args.csv)
    if not config_path.exists():
        raise AutomationError(f"Missing config file: {config_path}")
    if not csv_path.exists():
        raise AutomationError(f"Missing CSV file: {csv_path}")

    config = load_json(config_path)
    if args.days < 1:
        raise AutomationError("--days must be at least 1.")
    if args.start_time:
        parse_clock_time(args.start_time)
        if args.days != 1:
            raise AutomationError("--start-time can only be used with --days 1.")
    if args.fresh_login and not args.auto_login:
        raise AutomationError("--fresh-login requires --auto-login.")
    if args.submit and args.days > 1 and not args.allow_multi_day_submit:
        raise AutomationError(
            "Multi-day submit is blocked for safety. Coupang WING appears to accept the form "
            "but may silently omit product option links on later dates. Use --days 1, or run "
            "with --allow-multi-day-submit only after confirming Coupang allows this account/product setup."
        )
    start_date = (
        dt.date.fromisoformat(args.target_date)
        if args.target_date
        else dt.datetime.now(KST).date() + dt.timedelta(days=1)
    )
    target_dates = [start_date + dt.timedelta(days=offset) for offset in range(args.days)]
    append_date_suffix = args.append_date_suffix or args.days > 1

    with sync_playwright() as p:
        browser: Browser | None = None
        launch_options = browser_launch_options(config)
        browser_channel = selected_browser_channel(config)
        if browser_channel:
            log(f"Using browser channel: {browser_channel}")
        browser = p.chromium.launch(**launch_options)
        if args.fresh_login:
            log("Fresh login mode enabled. Saved browser storage state will be ignored for this run.")
        context = browser.new_context(**new_context_options(config, use_storage_state=not args.fresh_login))
        install_context_patches(context, config)
        page = context.pages[0] if context.pages else context.new_page()
        page.bring_to_front()
        try:
            if args.inspect:
                inspect_session(page, config)
                return 0

            rows = load_rows(csv_path)
            if args.manual_coupon_page:
                page.goto(config.get("wing_url", "https://wing.coupang.com"), wait_until="domcontentloaded")
                wait_for_manual_coupon_page(page)
            else:
                navigate_to_coupon_page(page, config)
                ensure_logged_in(page, context, config, auto_login=args.auto_login)
                if not coupon_form_is_present(page) and not create_button_is_visible(page, config, timeout=300):
                    navigate_to_coupon_page(page, config)
                    ensure_logged_in(page, context, config, auto_login=args.auto_login)
            results = []
            failures = []
            jobs = [(row, target_date) for row in rows for target_date in target_dates]
            for job_index, (row, target_date) in enumerate(jobs):
                try:
                    if job_index > 0 and not args.manual_coupon_page:
                        navigate_to_coupon_page(page, config)
                        ensure_logged_in(page, context, config, auto_login=args.auto_login)
                    results.append(
                        create_coupon(
                            page,
                            config,
                            row,
                            target_date,
                            submit=args.submit,
                            open_create_button=not args.manual_coupon_page,
                            append_date_suffix=append_date_suffix,
                            start_time=args.start_time,
                        )
                    )
                except Exception as exc:
                    save_artifacts(page, f"failed_line_{row['line']}_{target_date:%Y%m%d}")
                    failure = {
                        "line": row["line"],
                        "campaign_name": row.get("campaign_name", ""),
                        "target_date": str(target_date),
                        "submit": args.submit,
                        "vendor_item_count": len(row.get("vendor_item_ids", [])),
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    failures.append(failure)
                    results.append(failure)
                    log(f"Coupon failed but continuing: {failure['error']}")
                    if not args.continue_on_error:
                        raise
            print(RESULTS_BEGIN)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            print(RESULTS_END)
            return 1 if failures else 0
        finally:
            context.close()
            if browser:
                browser.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
