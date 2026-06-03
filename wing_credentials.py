#!/usr/bin/env python3
"""Environment credential helpers for Coupang WING login."""

from __future__ import annotations

import os


LOGIN_ID_ENV_KEYS = ("COUPANG_WING_ID", "WING_LOGIN_ID", "WING_ID")
LOGIN_PASSWORD_ENV_KEYS = ("COUPANG_WING_PASSWORD", "WING_LOGIN_PASSWORD", "WING_PASSWORD")


def _first_env(keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def load_env_credentials() -> tuple[str | None, str | None]:
    return _first_env(LOGIN_ID_ENV_KEYS), _first_env(LOGIN_PASSWORD_ENV_KEYS)


def load_credentials() -> tuple[str | None, str | None]:
    return load_env_credentials()


def credential_source() -> str:
    login_id, password = load_env_credentials()
    return "environment" if login_id and password else "none"


def has_credentials() -> bool:
    login_id, password = load_credentials()
    return bool(login_id and password)
