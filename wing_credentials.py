#!/usr/bin/env python3
"""Credential helpers for Coupang WING login.

Server runs should provide credentials through environment variables. macOS
desktop runs can also store them in Keychain through the local web UI.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ACCOUNT = "wing"
LOGIN_ID_SERVICE = "com.joon.coupang.wing.login-id"
LOGIN_PASSWORD_SERVICE = "com.joon.coupang.wing.password"
SECURITY = Path("/usr/bin/security")
LOGIN_ID_ENV_KEYS = ("COUPANG_WING_ID", "WING_LOGIN_ID", "WING_ID")
LOGIN_PASSWORD_ENV_KEYS = ("COUPANG_WING_PASSWORD", "WING_LOGIN_PASSWORD", "WING_PASSWORD")


class CredentialError(RuntimeError):
    pass


def _run_security(args: list[str]) -> subprocess.CompletedProcess[str]:
    if not SECURITY.exists():
        raise CredentialError("macOS Keychain command was not found: /usr/bin/security")
    return subprocess.run(
        [str(SECURITY), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _save_secret(service: str, value: str) -> None:
    completed = _run_security(
        ["add-generic-password", "-U", "-s", service, "-a", ACCOUNT, "-w", value]
    )
    if completed.returncode != 0:
        raise CredentialError(completed.stdout.strip() or "Failed to save credentials to Keychain.")


def _read_secret(service: str) -> str | None:
    try:
        completed = _run_security(["find-generic-password", "-s", service, "-a", ACCOUNT, "-w"])
    except CredentialError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _delete_secret(service: str) -> None:
    _run_security(["delete-generic-password", "-s", service, "-a", ACCOUNT])


def save_credentials(login_id: str, password: str) -> None:
    login_id = login_id.strip()
    if not login_id or not password:
        raise CredentialError("Login ID and password are required.")
    _save_secret(LOGIN_ID_SERVICE, login_id)
    _save_secret(LOGIN_PASSWORD_SERVICE, password)


def _first_env(keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def load_env_credentials() -> tuple[str | None, str | None]:
    return _first_env(LOGIN_ID_ENV_KEYS), _first_env(LOGIN_PASSWORD_ENV_KEYS)


def load_credentials() -> tuple[str | None, str | None]:
    env_login_id, env_password = load_env_credentials()
    if env_login_id and env_password:
        return env_login_id, env_password
    return _read_secret(LOGIN_ID_SERVICE), _read_secret(LOGIN_PASSWORD_SERVICE)


def credential_source() -> str:
    env_login_id, env_password = load_env_credentials()
    if env_login_id and env_password:
        return "environment"
    keychain_login_id = _read_secret(LOGIN_ID_SERVICE)
    keychain_password = _read_secret(LOGIN_PASSWORD_SERVICE)
    if keychain_login_id and keychain_password:
        return "keychain"
    return "none"


def has_credentials() -> bool:
    login_id, password = load_credentials()
    return bool(login_id and password)


def delete_credentials() -> None:
    _delete_secret(LOGIN_ID_SERVICE)
    _delete_secret(LOGIN_PASSWORD_SERVICE)
