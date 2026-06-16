"""設定・アカウントの永続化（ローカルファイル / Vercel KV）."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KV_SETTINGS_KEY = "mitene:settings"
KV_ACCOUNTS_KEY = "mitene:accounts"


def kv_available() -> bool:
    return bool(os.getenv("KV_REST_API_URL") and os.getenv("KV_REST_API_TOKEN"))


def storage_mode() -> str:
    """local | kv | ephemeral"""
    if os.getenv("VERCEL"):
        return "kv" if kv_available() else "ephemeral"
    return "local"


def storage_warning() -> str:
    if storage_mode() != "ephemeral":
        return ""
    return (
        "Vercel の一時領域に保存しているため、再読み込みで登録が消えます。"
        "Vercel ダッシュボード → Storage → KV を作成し、このプロジェクトに接続して再デプロイしてください。"
    )


def _redis():
    from upstash_redis import Redis

    return Redis(
        url=os.environ["KV_REST_API_URL"],
        token=os.environ["KV_REST_API_TOKEN"],
    )


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
            logger.exception("Vercel KV から settings を読めませんでした")
            return None
    return _read_local(path)


def write_settings(path: Path, data: dict[str, Any]) -> None:
    mode = storage_mode()
    if mode == "kv":
        _write_kv(KV_SETTINGS_KEY, data)
        return
    if mode == "ephemeral":
        logger.warning("Vercel KV 未設定: settings は再起動で消える可能性があります")
    _write_local(path, data)


def read_accounts(path: Path) -> dict[str, Any] | None:
    mode = storage_mode()
    if mode == "kv":
        try:
            return _read_kv(KV_ACCOUNTS_KEY)
        except Exception:
            logger.exception("Vercel KV から accounts を読めませんでした")
            return None
    return _read_local(path)


def write_accounts(path: Path, data: dict[str, Any]) -> None:
    mode = storage_mode()
    if mode == "kv":
        _write_kv(KV_ACCOUNTS_KEY, data)
        return
    if mode == "ephemeral":
        logger.warning("Vercel KV 未設定: accounts は再起動で消える可能性があります")
    _write_local(path, data)
