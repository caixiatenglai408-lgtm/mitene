"""設定・アカウントの永続化（ローカルファイル / Vercel Redis）."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KV_SETTINGS_KEY = "mitene:settings"
KV_ACCOUNTS_KEY = "mitene:accounts"

_REDIS_ENV_PAIRS = (
    ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
    ("KV_REST_API_URL", "KV_REST_API_TOKEN"),
)


def redis_credentials() -> tuple[str, str] | None:
    for url_key, token_key in _REDIS_ENV_PAIRS:
        url = (os.getenv(url_key) or "").strip()
        token = (os.getenv(token_key) or "").strip()
        if url and token:
            return url, token
    return None


def kv_available() -> bool:
    return redis_credentials() is not None


def storage_mode() -> str:
    """local | kv | ephemeral"""
    if os.getenv("VERCEL"):
        return "kv" if kv_available() else "ephemeral"
    return "local"


def storage_warning() -> str:
    if storage_mode() != "ephemeral":
        return ""
    hints: list[str] = []
    for url_key, token_key in _REDIS_ENV_PAIRS:
        if os.getenv(url_key) and not os.getenv(token_key):
            hints.append(f"{token_key} が未設定")
        elif os.getenv(token_key) and not os.getenv(url_key):
            hints.append(f"{url_key} が未設定")
    detail = f"（{', '.join(hints)}）" if hints else ""
    return (
        "Vercel の一時領域に保存しているため、再読み込みで登録が消えます。"
        "Storage で Redis を作成し、このプロジェクトに接続したあと Redeploy してください。"
        "Settings → Environment Variables に UPSTASH_REDIS_REST_URL / "
        "UPSTASH_REDIS_REST_TOKEN（または KV_REST_API_URL / KV_REST_API_TOKEN）"
        f"があるか確認してください。{detail}"
    )


def storage_debug() -> dict[str, Any]:
    creds = redis_credentials()
    return {
        "mode": storage_mode(),
        "vercel": bool(os.getenv("VERCEL")),
        "redis_configured": creds is not None,
        "env": {
            url_key: bool(os.getenv(url_key))
            for url_key, token_key in _REDIS_ENV_PAIRS
            for _ in ((),)
        }
        | {
            token_key: bool(os.getenv(token_key))
            for url_key, token_key in _REDIS_ENV_PAIRS
        },
    }


def _redis():
    from upstash_redis import Redis

    creds = redis_credentials()
    if creds:
        return Redis(url=creds[0], token=creds[1])
    try:
        return Redis.from_env()
    except Exception as exc:
        raise RuntimeError(
            "Redis 環境変数が見つかりません。"
            "UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN を設定してください。"
        ) from exc


def _read_local(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_local(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_kv(key: str) -> dict[str, Any] | None:
    raw = _redis().get(key)
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        return json.loads(raw)
    raise TypeError(f"unexpected KV payload type: {type(raw).__name__}")


def _write_kv(key: str, data: dict[str, Any]) -> None:
    _redis().set(key, json.dumps(data, ensure_ascii=False))


def read_settings(path: Path) -> dict[str, Any] | None:
    mode = storage_mode()
    if mode == "kv":
        try:
            return _read_kv(KV_SETTINGS_KEY)
        except Exception:
            logger.exception("Redis から settings を読めませんでした")
            return None
    return _read_local(path)


def write_settings(path: Path, data: dict[str, Any]) -> None:
    mode = storage_mode()
    if mode == "kv":
        _write_kv(KV_SETTINGS_KEY, data)
        return
    if mode == "ephemeral":
        logger.warning("Redis 未設定: settings は再起動で消える可能性があります")
    _write_local(path, data)


def read_accounts(path: Path) -> dict[str, Any] | None:
    mode = storage_mode()
    if mode == "kv":
        try:
            return _read_kv(KV_ACCOUNTS_KEY)
        except Exception:
            logger.exception("Redis から accounts を読めませんでした")
            return None
    return _read_local(path)


def write_accounts(path: Path, data: dict[str, Any]) -> None:
    mode = storage_mode()
    if mode == "kv":
        _write_kv(KV_ACCOUNTS_KEY, data)
        return
    if mode == "ephemeral":
        logger.warning("Redis 未設定: accounts は再起動で消える可能性があります")
    _write_local(path, data)
