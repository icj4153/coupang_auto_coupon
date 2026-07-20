#!/usr/bin/env python3
"""Export a Coupang WING session from a user-driven Chrome instance."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def storage_state_path(config_path: Path, config: dict[str, Any]) -> Path:
    raw_path = Path(config.get("storage_state_path", "wing_storage_state.json")).expanduser()
    if raw_path.is_absolute():
        return raw_path
    return (config_path.parent / raw_path).resolve()


def chrome_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError):
        return False


def wait_for_chrome(port: int, timeout_seconds: int = 20) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if chrome_ready(port):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Chrome remote debugging port did not open: {port}")


def launch_chrome(port: int, profile_dir: Path, url: str) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "open",
            "-na",
            "Google Chrome",
            "--args",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        check=True,
    )


def export_storage_state(port: int, output_path: Path) -> tuple[int, str, str]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        try:
            contexts = browser.contexts
            if not contexts:
                raise RuntimeError("No Chrome context found over CDP.")
            context = contexts[0]
            pages = context.pages
            active_page = pages[-1] if pages else None
            title = active_page.title() if active_page else ""
            url = active_page.url if active_page else ""
            output_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(output_path))
        finally:
            browser.close()

    state = json.loads(output_path.read_text(encoding="utf-8"))
    cookies = state.get("cookies", [])
    coupang_cookie_count = sum(1 for cookie in cookies if "coupang.com" in str(cookie.get("domain", "")))
    return coupang_cookie_count, title, url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open real Chrome for manual Coupang WING login and export Playwright storage state.",
    )
    parser.add_argument("--config", default="browser_coupon_config.json")
    parser.add_argument("--profile-dir", default=".chrome-wing-login-profile")
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--timeout-seconds", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    state_path = storage_state_path(config_path, config)
    start_url = config.get("wing_url") or "https://wing.coupang.com"
    profile_dir = Path(args.profile_dir).expanduser().resolve()

    print("[chrome-session] 일반 Chrome 앱을 로그인 전용 프로필로 엽니다.", flush=True)
    print(f"[chrome-session] Profile: {profile_dir}", flush=True)
    launch_chrome(args.port, profile_dir, start_url)
    wait_for_chrome(args.port, timeout_seconds=args.timeout_seconds)

    print("", flush=True)
    print("[chrome-session] 열린 Chrome에서 쿠팡 WING 로그인을 완료하세요.", flush=True)
    print("[chrome-session] WING 메인/쿠폰 페이지가 정상적으로 보이면 이 터미널에서 Enter를 누르세요.", flush=True)
    input()

    cookie_count, title, url = export_storage_state(args.port, state_path)
    print(f"[chrome-session] Current page: {title} / {url}", flush=True)
    print(f"[chrome-session] Coupang cookies exported: {cookie_count}", flush=True)
    if cookie_count <= 0:
        raise RuntimeError("Coupang cookies were not found. WING login may not be complete.")
    if "access denied" in title.lower():
        raise RuntimeError("Chrome is still on Access Denied. Try normal Chrome access again before exporting.")
    print(f"[chrome-session] Saved browser storage state: {state_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[chrome-session] cancelled", file=sys.stderr)
        raise SystemExit(130)
