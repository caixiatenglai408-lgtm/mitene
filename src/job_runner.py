"""手動実行ジョブ（バックグラウンド）."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from result_display import build_partial_errors_display, build_run_display
from store import save_last_run_report

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")


class JobCancelled(Exception):
    """手動中止."""


_jobs: dict[str, dict[str, Any]] = {}
_job_controls: dict[str, dict[str, bool]] = {}
_lock = threading.Lock()
_direct_run_depth = 0
_send_tls = threading.local()


def _now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def _running_label_for_job(
    *,
    dry_run: bool,
    job_type: str,
    group: str,
    account_name: str,
) -> str:
    if dry_run:
        return "ドライラン実行中…"
    if job_type == "single":
        name = (account_name or "").strip()
        return f"「{name}」送信中…" if name else "個別送信中…"
    if group == "working":
        return "出勤送信中…"
    if group == "off":
        return "お休み送信中…"
    return "全員送信中…"


def _init_job(
    job_id: str,
    name: str,
    *,
    dry_run: bool = False,
    job_type: str = "batch",
    group: str = "all",
    account_name: str = "",
) -> None:
    running_message = _running_label_for_job(
        dry_run=dry_run,
        job_type=job_type,
        group=group,
        account_name=account_name,
    )
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "name": name,
            "status": "running",
            "running": True,
            "completed": False,
            "dry_run": dry_run,
            "job_type": job_type,
            "group": group,
            "account_name": account_name,
            "started_at": _now(),
            "finished_at": None,
            "results": None,
            "error": None,
            "message": running_message,
            "progress": None,
            "partial_results": None,
            "partial_display": None,
            "control": {"paused": False, "cancelled": False},
        }
        _job_controls[job_id] = {"paused": False, "cancelled": False}


def _cleanup_job_control(job_id: str) -> None:
    with _lock:
        _job_controls.pop(job_id, None)


def get_job_control(job_id: str) -> dict[str, bool]:
    with _lock:
        ctrl = _job_controls.get(job_id)
        if not ctrl:
            return {"paused": False, "cancelled": False}
        return dict(ctrl)


def pause_job(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        ctrl = _job_controls.get(job_id)
        if not job or job.get("status") != "running" or not ctrl:
            return False
        if ctrl.get("cancelled"):
            return False
        ctrl["paused"] = True
        job["control"] = dict(ctrl)
        job["message"] = "送信を一時停止中…"
        logger.info("ジョブ %s を一時停止", job_id)
        return True


def resume_job(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        ctrl = _job_controls.get(job_id)
        if not job or job.get("status") != "running" or not ctrl:
            return False
        if ctrl.get("cancelled"):
            return False
        ctrl["paused"] = False
        job["control"] = dict(ctrl)
        job["message"] = "ドライラン実行中…" if job.get("dry_run") else "実行中…"
        logger.info("ジョブ %s を再開", job_id)
        return True


def cancel_job(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        ctrl = _job_controls.get(job_id)
        if not job or job.get("status") != "running" or not ctrl:
            return False
        ctrl["cancelled"] = True
        ctrl["paused"] = False
        job["control"] = dict(ctrl)
        job["message"] = "中止しています…"
        return True


def wait_if_paused(job_id: str | None) -> None:
    """一時停止中は待機。中止なら JobCancelled."""
    if not job_id:
        return
    while True:
        with _lock:
            ctrl = _job_controls.get(job_id, {})
            if ctrl.get("cancelled"):
                raise JobCancelled()
            paused = bool(ctrl.get("paused"))
            if paused:
                job = _jobs.get(job_id)
                if job and job.get("status") == "running":
                    job["message"] = "送信を一時停止中…"
        if not paused:
            return
        time.sleep(0.25)


def interruptible_sleep(seconds: float, *, job_id: str | None = None) -> None:
    """停止・中止を反映できる sleep（手動送信中のみ job_id あり）."""
    if seconds <= 0:
        wait_if_paused(job_id or get_current_job_id())
        return
    deadline = time.monotonic() + seconds
    jid = job_id or get_current_job_id()
    while True:
        wait_if_paused(jid)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.25, remaining))


def get_current_job_id() -> str | None:
    job_id = getattr(_send_tls, "job_id", None)
    return str(job_id) if job_id else None


def report_send_progress(
    job_id: str | None,
    *,
    account_name: str,
    account_id: str = "",
    send_done: int = 0,
    send_budget: int = 0,
) -> None:
    """1人送信中の進捗（送信完了件数 / ミテネ残り回数）."""
    if not job_id:
        return
    if send_budget > 0:
        message = f"送信中：{account_name}（{send_done}/{send_budget}）"
    else:
        message = f"送信中：{account_name}…"
    payload: dict[str, Any] = {
        "message": message,
        "progress": {
            "account_name": account_name,
            "account_id": account_id,
            "send_done": send_done,
            "send_budget": send_budget,
        },
    }
    with _lock:
        job = _jobs.get(job_id)
        if job and job.get("status") == "running":
            job.update(payload)


def bind_send_progress(
    job_id: str | None, account_id: str, account_name: str
) -> None:
    if not job_id:
        return

    def callback(send_done: int, send_budget: int) -> None:
        report_send_progress(
            job_id,
            account_name=account_name,
            account_id=account_id,
            send_done=send_done,
            send_budget=send_budget,
        )

    _send_tls.callback = callback
    _send_tls.job_id = job_id
    report_send_progress(
        job_id,
        account_name=account_name,
        account_id=account_id,
        send_done=0,
        send_budget=0,
    )


def get_send_progress_callback() -> Callable[[int, int], None] | None:
    return getattr(_send_tls, "callback", None)


def clear_send_progress() -> None:
    _send_tls.callback = None
    _send_tls.job_id = None


def report_job_progress(
    job_id: str | None,
    *,
    account_name: str,
    account_id: str = "",
    partial_results: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> None:
    if not job_id:
        return
    payload: dict[str, Any] = {}
    if partial_results is not None:
        payload["partial_results"] = partial_results
        payload["partial_display"] = build_partial_errors_display(
            partial_results, dry_run=dry_run
        )
        payload["completed_account_ids"] = [
            str(r["account_id"])
            for r in partial_results
            if r.get("account_id")
        ]
    with _lock:
        job = _jobs.get(job_id)
        if job and job.get("status") == "running":
            job.update(payload)


def get_active_job_summary() -> dict[str, Any] | None:
    """送信中ジョブの種別（heartbeat / UI 用）."""
    with _lock:
        for job in _jobs.values():
            if job.get("status") != "running":
                continue
            return {
                "job_type": job.get("job_type") or "batch",
                "group": job.get("group") or "all",
                "account_name": job.get("account_name") or "",
                "name": job.get("name") or "",
                "message": job.get("message") or "",
                "dry_run": bool(job.get("dry_run")),
                "running": True,
                "completed": False,
            }
    return None


def _finalize_background_job(
    job_id: str,
    *,
    results: list[dict[str, Any]] | None,
    dry_run: bool,
    cancelled: bool = False,
    error_msg: str | None = None,
) -> None:
    """ジョブ終了（成功・失敗・中止を問わず必ず status=done / completed=True）."""
    control = get_job_control(job_id)
    with _lock:
        stored = _jobs.get(job_id) or {}
        if not results:
            results = list(stored.get("partial_results") or [])

    dry = dry_run or bool(results and results[0].get("dry_run"))
    display: dict[str, Any]
    try:
        if results:
            display = build_run_display(results, dry_run=dry)
        else:
            detail = error_msg or "送信に失敗しました"
            display = {
                "summary": detail,
                "completed": [],
                "errors": [{"name": "全体", "detail": detail}],
                "has_issues": True,
            }
    except Exception:
        logger.warning("ジョブ結果表示の組み立てに失敗", exc_info=True)
        detail = error_msg or "送信に失敗しました"
        display = {
            "summary": detail,
            "completed": [],
            "errors": [{"name": "全体", "detail": detail}],
            "has_issues": True,
        }

    summary = str(display.get("summary") or "処理が終わりました")
    if cancelled:
        summary = f"中止: {summary}"

    if results:
        try:
            save_last_run_report("manual", results, dry_run=dry)
        except Exception:
            logger.warning("送信結果の保存に失敗しました", exc_info=True)

    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(
            {
                "status": "done",
                "running": False,
                "completed": True,
                "finished_at": _now(),
                "results": results,
                "display": display,
                "message": summary,
                "error": error_msg,
                "progress": None,
                "partial_display": None,
                "partial_results": results,
                "control": control,
            }
        )


def start_background_job(
    name: str,
    fn: Callable[[str], list[dict]],
    *,
    dry_run: bool = False,
    job_type: str = "batch",
    group: str = "all",
    account_name: str = "",
) -> str:
    job_id = str(uuid.uuid4())
    _init_job(
        job_id,
        name,
        dry_run=dry_run,
        job_type=job_type,
        group=group,
        account_name=account_name,
    )

    def worker() -> None:
        from sleep_guard import acquire, release

        acquire()
        results: list[dict[str, Any]] = []
        cancelled = False
        error_msg: str | None = None
        try:
            try:
                results = fn(job_id) or []
            except JobCancelled:
                cancelled = True
                with _lock:
                    results = list(
                        (_jobs.get(job_id) or {}).get("partial_results") or []
                    )
            except Exception as e:
                logger.exception("%s: ジョブ失敗", name)
                error_msg = str(e)
                with _lock:
                    results = list(
                        (_jobs.get(job_id) or {}).get("partial_results") or []
                    )
            if not cancelled:
                cancelled = bool(get_job_control(job_id).get("cancelled"))
        finally:
            _finalize_background_job(
                job_id,
                results=results,
                dry_run=dry_run,
                cancelled=cancelled,
                error_msg=error_msg,
            )
            _cleanup_job_control(job_id)
            release()

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        out = dict(job)
        ctrl = _job_controls.get(job_id)
        if ctrl:
            out["control"] = dict(ctrl)
        return out


def has_running_jobs() -> bool:
    with _lock:
        return any(job.get("status") == "running" for job in _jobs.values())


def run_with_busy_guard(fn: Callable[[], Any]) -> Any:
    """1アカウント送信など、ジョブ外の長時間処理を busy として扱う."""
    from sleep_guard import acquire, release

    global _direct_run_depth
    with _lock:
        _direct_run_depth += 1
    acquire()
    try:
        return fn()
    finally:
        release()
        with _lock:
            _direct_run_depth -= 1


def is_system_busy() -> bool:
    with _lock:
        if _direct_run_depth > 0:
            return True
    return has_running_jobs()


def validate_before_run(group: str = "all") -> str | None:
    from store import accounts_for_manual_group, load_accounts, load_settings

    settings = load_settings()
    if not settings.base_url:
        return "ログインURLが未設定です。「女の子登録情報」で保存してください。"
    enabled = [a for a in load_accounts() if a.enabled]
    if not enabled:
        return "有効な女の子が登録されていません。「女の子登録情報」で追加してください。"
    targets = accounts_for_manual_group(group)
    if not targets:
        if group == "working":
            return "本日出勤の女の子がいません。登録一覧でチェックしてください。"
        if group == "off":
            return "お休みの女の子がいません。"
    return None
