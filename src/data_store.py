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

_REDIS_REST_ENV_PAIRS = (
    ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
    ("KV_REST_API_URL", "KV_REST_API_TOKEN"),
)

_redis_url_client: Any | None = None


class StorageNotConfiguredError(RuntimeError):
    """Vercel 上で Redis 未接続のときの保存拒否."""


def redis_rest_credentials() -> tuple[str, str] | None:
    for url_key, token_key in _REDIS_REST_ENV_PAIRS:
        url = (os.getenv(url_key) or "").strip()
        token = (os.getenv(token_key) or "").strip()
        if url and token:
            return url, token
    return None


def redis_url() -> str | None:
    value = (os.getenv("REDIS_URL") or "").strip()
    return value or None


def redis_backend() -> str | None:
    """rest | url"""
    if redis_rest_credentials():
        return "rest"
    if redis_url():
        return "url"
    return None


def kv_available() -> bool:
    return redis_backend() is not None


def storage_mode() -> str:
    """local | kv | ephemeral"""
    if os.getenv("VERCEL"):
        return "kv" if kv_available() else "ephemeral"
    return "local"


def storage_warning() -> str:
    if storage_mode() != "ephemeral":
        return ""
    return (
        "【重要】Redis 未接続のため、登録はサーバーごとの一時メモリに入り、"
        "リロードやタブを開き直すと出たり消えたりします。"
        "いまの Redis 画面右上の「Connect to Project」からプロジェクト mitene を選び、"
        "接続後に Deployments → Redeploy してください。"
        "REDIS_URL または UPSTASH_REDIS_REST_URL が入れば黄色い警告は消えます。"
    )


def _block_ephemeral_write() -> None:
    if storage_mode() == "ephemeral":
        raise StorageNotConfiguredError(storage_warning())


def storage_debug() -> dict[str, Any]:
    return {
        "mode": storage_mode(),
        "vercel": bool(os.getenv("VERCEL")),
        "redis_configured": kv_available(),
        "redis_backend": redis_backend(),
        "env": {
            "REDIS_URL": bool(redis_url()),
            **{
                key: bool(os.getenv(key))
                for pair in _REDIS_REST_ENV_PAIRS
                for key in pair
            },
        },
    }


def _redis_rest_client():
    from upstash_redis import Redis

    creds = redis_rest_credentials()
    if creds:
        return Redis(url=creds[0], token=creds[1])
    return Redis.from_env()


def _redis_url_client_instance():
    global _redis_url_client
    if _redis_url_client is None:
        import redis

        _redis_url_client = redis.from_url(redis_url(), decode_responses=True)
    return _redis_url_client


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


def _decode_kv_payload(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        return json.loads(raw)
    raise TypeError(f"unexpected KV payload type: {type(raw).__name__}")


def _read_kv(key: str) -> dict[str, Any] | None:
    backend = redis_backend()
    if backend == "url":
        return _decode_kv_payload(_redis_url_client_instance().get(key))
    if backend == "rest":
        return _decode_kv_payload(_redis_rest_client().get(key))
    raise RuntimeError("Redis が未設定です")


def _write_kv(key: str, data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    backend = redis_backend()
    if backend == "url":
        _redis_url_client_instance().set(key, payload)
        return
    if backend == "rest":
        _redis_rest_client().set(key, payload)
        return
    raise RuntimeError("Redis が未設定です")


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
        _block_ephemeral_write()
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
        _block_ephemeral_write()
    _write_local(path, data)
