"""Vercel 管理画面 → 送信ワーカー（Railway 等）への委譲."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def worker_enabled() -> bool:
    return bool((os.getenv("MITENE_WORKER_URL") or "").strip())


def worker_base_url() -> str:
    return (os.getenv("MITENE_WORKER_URL") or "").strip().rstrip("/")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    secret = (os.getenv("MITENE_WORKER_SECRET") or "").strip()
    if secret:
        headers["X-Worker-Secret"] = secret
    return headers


def _request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: int = 1200,
) -> dict[str, Any]:
    if not worker_enabled():
        raise RuntimeError("MITENE_WORKER_URL が未設定です")
    url = f"{worker_base_url()}{path}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=_headers(), method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            message = parsed.get("error") or parsed.get("message") or detail
        except json.JSONDecodeError:
            message = detail or str(e)
        raise RuntimeError(f"送信ワーカーエラー ({e.code}): {message}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"送信ワーカーに接続できません ({worker_base_url()}): {e.reason}"
        ) from e


def worker_start_account_job(
    account_id: str,
    *,
    dry_run: bool = False,
    respect_enabled: bool = True,
) -> str:
    data = _request(
        "POST",
        f"/jobs/run-account/{account_id}",
        payload={"dry_run": dry_run, "respect_enabled": respect_enabled},
        timeout=60,
    )
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "ジョブ開始に失敗しました")
    job_id = data.get("job_id")
    if not job_id:
        raise RuntimeError("ジョブIDを取得できませんでした")
    return str(job_id)


def worker_run_account(
    account_id: str,
    *,
    dry_run: bool = False,
    respect_enabled: bool = True,
) -> dict[str, Any]:
    data = _request(
        "POST",
        f"/run/account/{account_id}",
        payload={"dry_run": dry_run, "respect_enabled": respect_enabled},
    )
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "送信ワーカーが失敗しました")
    return data.get("result") or {}


def worker_start_job(name: str, *, dry_run: bool = False, group: str = "all") -> str:
    data = _request(
        "POST",
        "/jobs/run-all",
        payload={"name": name, "dry_run": dry_run, "group": group},
        timeout=60,
    )
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "ジョブ開始に失敗しました")
    job_id = data.get("job_id")
    if not job_id:
        raise RuntimeError("ジョブIDを取得できませんでした")
    return str(job_id)


def worker_get_job(job_id: str) -> dict[str, Any] | None:
    data = _request("GET", f"/jobs/{job_id}", timeout=30)
    if not data.get("ok"):
        return None
    job = data.get("job")
    return job if isinstance(job, dict) else None


def worker_system_busy() -> bool:
    """送信ワーカー側に実行中ジョブがあるか（UI busy 統合用）.

    取得に失敗した場合はフェイルセーフで True（通信障害時に idle 誤判定しない）.
    """
    if not worker_enabled():
        return False
    try:
        data = _request("GET", "/system/busy", timeout=10)
    except RuntimeError:
        return True
    if not data.get("ok"):
        return True
    return bool(data.get("busy"))


def worker_pause_job(job_id: str) -> bool:
    data = _request("POST", f"/jobs/{job_id}/pause", timeout=30)
    return bool(data.get("ok"))


def worker_resume_job(job_id: str) -> bool:
    data = _request("POST", f"/jobs/{job_id}/resume", timeout=30)
    return bool(data.get("ok"))


def worker_cancel_job(job_id: str) -> bool:
    data = _request("POST", f"/jobs/{job_id}/cancel", timeout=30)
    return bool(data.get("ok"))
