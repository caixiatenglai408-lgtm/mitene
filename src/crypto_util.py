"""パスワードのローカル暗号化（任意）."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet | None:
    key = os.getenv("MITENE_SECRET_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        return None


def encrypt_secret(value: str) -> str:
    f = _fernet()
    if not f or not value:
        return value
    return f.encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    f = _fernet()
    if not f or not value:
        return value
    try:
        return f.decrypt(value.encode()).decode()
    except InvalidToken:
        return value


