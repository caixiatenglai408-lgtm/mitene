"""手動実行ジョブ（バックグラウンド）."""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from result_display import build_run_display
from store import save_last_run_report

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
# 手動送信の上限（Playwright がハングしたとき UI が「実行中」のままにならないように）
JOB_TIMEOUT_SEC = 20 * 60

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_direct_run_depth = 0


def _now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def start_background_job(name: str, fn: Callable[[], list[dict]]) -> str:
    job_id = str(uuid.uuid4())
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "name": name,
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "results": None,
            "error": None,
            "message": "実行中…",
        }

    def worker() -> None:
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(fn)
                results = future.result(timeout=JOB_TIMEOUT_SEC)
            dry_run = bool(results and results[0].get("dry_run"))
            display = build_run_display(results, dry_run=dry_run)
            save_last_run_report("manual", results, dry_run=dry_run)
            with _lock:
                _jobs[job_id].update(
                    {
                        "status": "done",
                        "finished_at": _now(),
                        "results": results,
                        "display": display,
                        "message": display["summary"],
                    }
                )
        except FuturesTimeoutError:
            msg = (
                f"処理が {JOB_TIMEOUT_SEC // 60} 分を超えたため中断しました。"
                "ターミナルのログを確認し、起動.command を再起動してから再実行してください。"
            )
            logger.error("%s: %s", name, msg)
            with _lock:
                _jobs[job_id].update(
                    {
                        "status": "error",
                        "finished_at": _now(),
                        "error": msg,
                        "message": msg,
                    }
                )
        except Exception as e:
            logger.exception("%s: ジョブ失敗", name)
            with _lock:
                _jobs[job_id].update(
                    {
                        "status": "error",
                        "finished_at": _now(),
                        "error": str(e),
                        "message": str(e),
                    }
                )

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def has_running_jobs() -> bool:
    with _lock:
        return any(job.get("status") == "running" for job in _jobs.values())


def run_with_busy_guard(fn: Callable[[], Any]) -> Any:
    """1アカウント送信など、ジョブ外の長時間処理を busy として扱う."""
    global _direct_run_depth
    with _lock:
        _direct_run_depth += 1
    try:
        return fn()
    finally:
        with _lock:
            _direct_run_depth -= 1


def is_system_busy() -> bool:
    with _lock:
        if _direct_run_depth > 0:
            return True
    if has_running_jobs():
        return True
    from scheduler_service import is_scheduled_run_in_progress

    return is_scheduled_run_in_progress()


def validate_before_run() -> str | None:
    from store import load_accounts, load_settings

    settings = load_settings()
    if not settings.base_url:
        return "ログインURLが未設定です。「女の子ログイン」で保存してください。"
    enabled = [a for a in load_accounts() if a.enabled]
    if not enabled:
        return "有効な女の子が登録されていません。「女の子ログイン」で追加してください。"
    return None
