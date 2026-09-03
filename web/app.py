"""ミテネ自動送信 管理画面."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

_src = Path(__file__).resolve().parent.parent / "src"
if _src.exists():
    sys.path.insert(0, str(_src))
try:
    from app_paths import APP_ROOT, BUNDLE_ROOT, is_frozen, setup_runtime
except ImportError:
    APP_ROOT = Path(__file__).resolve().parent.parent
    BUNDLE_ROOT = APP_ROOT

    def is_frozen() -> bool:
        return False

    def setup_runtime() -> None:
        pass

ROOT = APP_ROOT
setup_runtime()

from job_runner import get_job, is_system_busy, start_background_job, validate_before_run  # noqa: E402
from runner import run_for_account  # noqa: E402
from scheduler_service import run_manual_batch, run_single_account, start_scheduler  # noqa: E402
from store import (  # noqa: E402
    JST,
    attendance_today_label,
    load_working_today_ids,
    partition_enabled_by_attendance,
    reset_working_today,
    set_account_working_today,
    delete_account,
    get_account,
    has_duplicate_name,
    load_accounts,
    load_settings,
    save_settings,
    set_account_enabled,
    set_automation,
    toggle_schedule,
    upsert_account,
)

load_dotenv(ROOT / ".env")

_WEB_DIR = Path(__file__).resolve().parent
if is_frozen():
    _template_dir = BUNDLE_ROOT / "web" / "templates"
    _static_dir = BUNDLE_ROOT / "web" / "static"
else:
    _template_dir = _WEB_DIR / "templates"
    _static_dir = _WEB_DIR / "static"

app = Flask(
    __name__,
    template_folder=str(_template_dir),
    static_folder=str(_static_dir),
)
app.secret_key = __import__("os").getenv("FLASK_SECRET_KEY", "mitene-local-dev-key")
logging.basicConfig(level=logging.INFO)

_CLIENT_TTL_SEC = 120.0
_CLIENT_TTL_BUSY_SEC = 600.0
_client_tabs: dict[str, float] = {}
_client_lock = threading.Lock()


def _auto_exit_enabled() -> bool:
    return os.getenv("MITENE_AUTO_EXIT") == "1"


def _prune_client_tabs(*, grace: bool = False) -> int:
    now = time.time()
    ttl = _CLIENT_TTL_BUSY_SEC if grace else _CLIENT_TTL_SEC
    with _client_lock:
        stale = [k for k, t in _client_tabs.items() if now - t > ttl]
        for key in stale:
            del _client_tabs[key]
        return len(_client_tabs)


def _touch_client_tab(tab_id: str) -> None:
    with _client_lock:
        _client_tabs[tab_id] = time.time()


def _remove_client_tab(tab_id: str) -> None:
    with _client_lock:
        _client_tabs.pop(tab_id, None)


def _use_worker() -> bool:
    from worker_client import worker_enabled

    return worker_enabled()


def _worker_required_error() -> str:
    return (
        "Vercel から送信するには送信ワーカー（Railway 等）の設定が必要です。"
        "MITENE_WORKER_URL を Vercel の Environment Variables に追加してください。"
        "手順は README の「Vercel + 送信ワーカー」を参照してください。"
    )


def _combined_system_busy() -> bool:
    """UI プロセスと送信ワーカー双方の実行中状態."""
    if is_system_busy():
        return True
    if not _use_worker():
        return False
    try:
        from worker_client import worker_system_busy

        return worker_system_busy()
    except Exception:
        logging.exception("送信ワーカーの busy 取得に失敗")
        return True


@app.context_processor
def inject_ui_config():
    from app_paths import is_vercel
    from data_store import storage_mode, storage_warning
    from worker_client import worker_enabled

    vercel_send_warning = ""
    if is_vercel() and not worker_enabled():
        vercel_send_warning = _worker_required_error()
    return {
        "mitene_auto_exit": _auto_exit_enabled(),
        "storage_mode": storage_mode(),
        "storage_warning": storage_warning(),
        "vercel_send_warning": vercel_send_warning,
    }


@app.route("/manifest.webmanifest")
def manifest_redirect():
    return app.send_static_file("manifest.webmanifest")


@app.route("/portal-preview")
def portal_preview():
    """TMG Portal 用アイコンのプレビュー."""
    return app.send_static_file("portal-tile-preview.html")


@app.route("/")
def index():
    settings = load_settings()
    working_today, off_today = partition_enabled_by_attendance()
    return render_template(
        "index.html",
        settings=settings,
        accounts=load_accounts(),
        working_today=working_today,
        off_today=off_today,
        attendance_label=attendance_today_label(),
        working_today_ids=load_working_today_ids(),
    )


@app.route("/accounts", methods=["GET", "POST"])
def accounts():
    settings = load_settings()
    message = None
    error = None

    if request.method == "POST":
        action = request.form.get("action", "save")
        account_id = request.form.get("account_id", "").strip() or None
        name = request.form.get("name", "")
        login_id = request.form.get("login_id", "")
        password = request.form.get("password", "")
        enabled = request.form.get("enabled") == "on"
        base_url = request.form.get("base_url", "").strip()

        try:
            if action == "save_url":
                if not base_url:
                    error = "ログインURLを入力してください"
                else:
                    settings.base_url = base_url
                    save_settings(settings)
                    message = "ログインURLを保存しました"
            elif action == "delete" and account_id:
                acc = get_account(account_id)
                delete_account(account_id)
                label = acc.name if acc else "アカウント"
                message = f"「{label}」を削除しました"
            elif action == "save":
                if not name or not login_id:
                    error = "表示名とログインIDは必須です"
                elif not account_id and not password:
                    error = "新規登録時はパスワードが必須です"
                elif has_duplicate_name(name, account_id) and (
                    request.form.get("confirm_duplicate") != "1"
                ):
                    error = "同姓同名が存在します。登録する場合は確認ダイアログから実行してください"
                else:
                    upsert_account(name, login_id, password, account_id, enabled)
                    message = "保存しました"
        except Exception as e:
            error = str(e)

    return render_template(
        "accounts.html",
        settings=load_settings(),
        accounts=load_accounts(),
        message=message,
        error=error,
    )


@app.delete("/api/accounts/<account_id>")
def api_delete_account(account_id: str):
    acc = get_account(account_id)
    if not acc:
        return jsonify({"ok": False, "error": "アカウントが見つかりません"}), 404
    delete_account(account_id)
    return jsonify({"ok": True, "message": f"「{acc.name}」を削除しました"})


@app.post("/api/automation")
def api_automation():
    enabled = request.json.get("enabled", False)
    settings = set_automation(bool(enabled))
    return jsonify({"ok": True, "automation_enabled": settings.automation_enabled})


@app.post("/api/schedule/toggle")
def api_schedule_toggle():
    data = request.json or {}
    day = data.get("day", "")
    slot = data.get("slot", "")
    try:
        settings = toggle_schedule(day, slot)
        return jsonify(
            {
                "ok": True,
                "day": day,
                "slot": slot,
                "enabled": settings.schedule[day][slot],
                "schedule": settings.schedule,
            }
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.post("/api/run-now")
def api_run_now():
    data = request.json or {}
    group = str(data.get("group") or "all")
    if group not in ("all", "working", "off"):
        return jsonify({"ok": False, "error": "group は all / working / off です"}), 400
    err = validate_before_run(group)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    job_names = {
        "all": "今すぐ全員送信",
        "working": "本日出勤の女の子を送信",
        "off": "お休みの女の子を送信",
    }
    job_name = job_names[group]
    if _use_worker():
        try:
            from worker_client import worker_start_job

            job_id = worker_start_job(job_name, dry_run=False, group=group)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        if os.getenv("VERCEL"):
            return jsonify({"ok": False, "error": _worker_required_error()}), 400
        job_id = start_background_job(
            job_name,
            lambda jid, g=group: run_manual_batch(
                group=g, dry_run=False, job_id=jid
            ),
            dry_run=False,
            job_type="batch",
            group=group,
        )
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "job_type": "batch",
            "group": group,
            "message": "送信を開始しました。完了まで数分〜数十分かかることがあります。",
        }
    )


@app.post("/api/run-dry-all")
def api_run_dry_all():
    data = request.json or {}
    group = str(data.get("group") or "all")
    if group not in ("all", "working", "off"):
        return jsonify({"ok": False, "error": "group は all / working / off です"}), 400
    err = validate_before_run(group)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    job_names = {
        "all": "全員ドライラン",
        "working": "出勤グループ・ドライラン",
        "off": "お休みグループ・ドライラン",
    }
    job_name = job_names[group]
    if _use_worker():
        try:
            from worker_client import worker_start_job

            job_id = worker_start_job(job_name, dry_run=True, group=group)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        if os.getenv("VERCEL"):
            return jsonify({"ok": False, "error": _worker_required_error()}), 400
        job_id = start_background_job(
            job_name,
            lambda jid, g=group: run_manual_batch(
                group=g, dry_run=True, job_id=jid
            ),
            dry_run=True,
            job_type="batch",
            group=group,
        )
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "message": "ドライランを開始しました。",
        }
    )


@app.get("/api/jobs/<job_id>")
def api_job_status(job_id: str):
    if _use_worker():
        try:
            from worker_client import worker_get_job

            job = worker_get_job(job_id)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        job = get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "ジョブが見つかりません"}), 404
    return jsonify({"ok": True, "job": job})


@app.post("/api/jobs/<job_id>/pause")
def api_job_pause(job_id: str):
    if _use_worker():
        try:
            from worker_client import worker_pause_job

            ok = worker_pause_job(job_id)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        from job_runner import pause_job

        ok = pause_job(job_id)
    if not ok:
        return jsonify({"ok": False, "error": "一時停止できません"}), 400
    return jsonify({"ok": True, "paused": True})


@app.post("/api/jobs/<job_id>/resume")
def api_job_resume(job_id: str):
    if _use_worker():
        try:
            from worker_client import worker_resume_job

            ok = worker_resume_job(job_id)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        from job_runner import resume_job

        ok = resume_job(job_id)
    if not ok:
        return jsonify({"ok": False, "error": "再開できません"}), 400
    return jsonify({"ok": True, "paused": False})


@app.post("/api/jobs/<job_id>/cancel")
def api_job_cancel(job_id: str):
    if _use_worker():
        try:
            from worker_client import worker_cancel_job

            ok = worker_cancel_job(job_id)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        from job_runner import cancel_job

        ok = cancel_job(job_id)
    if not ok:
        return jsonify({"ok": False, "error": "中止できません"}), 400
    return jsonify({"ok": True, "cancelled": True})


@app.post("/api/run-test/<account_id>")
def api_run_test(account_id: str):
    settings = load_settings()
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "error": "アカウントが見つかりません"}), 404
    if not settings.base_url:
        return jsonify({"ok": False, "error": "ログインURLを設定してください"}), 400
    try:
        if _use_worker():
            from worker_client import worker_run_account

            result = worker_run_account(
                account_id, dry_run=True, respect_enabled=False
            )
        else:
            if os.getenv("VERCEL"):
                return jsonify({"ok": False, "error": _worker_required_error()}), 400
            result = run_for_account(
                account, settings.base_url, dry_run=True, headed=False
            )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "result": result})


@app.post("/api/run-account/<account_id>")
def api_run_account(account_id: str):
    settings = load_settings()
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "error": "アカウントが見つかりません"}), 404
    if not settings.base_url:
        return jsonify({"ok": False, "error": "ログインURLを設定してください"}), 400
    job_name = f"「{account.name}」を送信"
    try:
        if _use_worker():
            from worker_client import worker_start_account_job

            job_id = worker_start_account_job(
                account_id, dry_run=False, respect_enabled=False
            )
        else:
            if os.getenv("VERCEL"):
                return jsonify({"ok": False, "error": _worker_required_error()}), 400
            job_id = start_background_job(
                job_name,
                lambda jid, aid=account_id: run_single_account(
                    aid, dry_run=False, respect_enabled=False, job_id=jid
                ),
                dry_run=False,
                job_type="single",
                account_name=account.name,
            )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "job_type": "single",
            "account_name": account.name,
            "message": "送信を開始しました。",
        }
    )


@app.post("/api/attendance/reset")
def api_attendance_reset():
    settings = reset_working_today()
    working, off = partition_enabled_by_attendance()
    return jsonify(
        {
            "ok": True,
            "attendance_date": settings.attendance_date,
            "working_today_ids": list(settings.working_today_ids),
            "working_today": [a.to_public() for a in working],
            "off_today": [a.to_public() for a in off],
            "attendance_label": attendance_today_label(),
        }
    )


@app.post("/api/attendance/<account_id>")
def api_attendance(account_id: str):
    data = request.json or {}
    if "working" not in data:
        return jsonify({"ok": False, "error": "working を指定してください"}), 400
    try:
        settings = set_account_working_today(account_id, bool(data["working"]))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    working, off = partition_enabled_by_attendance()
    return jsonify(
        {
            "ok": True,
            "account_id": account_id,
            "working": bool(data["working"]),
            "attendance_date": settings.attendance_date,
            "working_today_ids": list(settings.working_today_ids),
            "working_today": [a.to_public() for a in working],
            "off_today": [a.to_public() for a in off],
            "attendance_label": attendance_today_label(),
        }
    )


@app.post("/api/accounts/<account_id>/enabled")
def api_account_enabled(account_id: str):
    data = request.json or {}
    if "enabled" not in data:
        return jsonify({"ok": False, "error": "enabled を指定してください"}), 400
    try:
        account = set_account_enabled(account_id, bool(data["enabled"]))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    label = "自動送信対象にしました" if account.enabled else "自動送信対象外にしました"
    return jsonify(
        {
            "ok": True,
            "enabled": account.enabled,
            "message": f"「{account.name}」を{label}",
        }
    )


@app.get("/api/status")
def api_status():
    """後方互換（再同期は行わない）."""
    return jsonify(_build_data_payload())


@app.get("/api/data")
def api_data():
    """設定・登録一覧の最新状態（画面の定期更新用）."""
    return jsonify(_build_data_payload())


@app.post("/api/client-heartbeat")
def api_client_heartbeat():
    """管理画面タブの生存通知（MITENE_AUTO_EXIT=1 のときのみ有効）."""
    if not _auto_exit_enabled():
        return jsonify({"ok": True, "auto_exit": False})
    payload = request.get_json(silent=True) or {}
    tab_id = str(payload.get("tab_id") or request.args.get("tab_id") or "").strip()
    if not tab_id:
        return jsonify({"ok": False, "error": "tab_id required"}), 400
    client_busy = bool(payload.get("busy"))
    _touch_client_tab(tab_id)
    busy = _combined_system_busy() or client_busy
    return jsonify({"ok": True, "active_count": _prune_client_tabs(grace=busy), "busy": busy})


@app.route("/api/client-heartbeat/leave", methods=["POST", "GET"])
def api_client_heartbeat_leave():
    if not _auto_exit_enabled():
        return jsonify({"ok": True, "auto_exit": False})
    payload = request.get_json(silent=True) or {}
    tab_id = str(
        payload.get("tab_id") or request.args.get("tab_id") or ""
    ).strip()
    if tab_id:
        _remove_client_tab(tab_id)
    busy = _combined_system_busy()
    return jsonify({"ok": True, "active_count": _prune_client_tabs(grace=busy)})


@app.get("/api/client-heartbeat/status")
def api_client_heartbeat_status():
    from job_runner import get_active_job_summary

    busy = _combined_system_busy()
    count = _prune_client_tabs(grace=busy)
    active_job = get_active_job_summary()
    payload: dict = {
        "ok": True,
        "active_count": count,
        "auto_exit": _auto_exit_enabled(),
        "busy": busy,
        "running": busy,
        "completed": not busy,
    }
    if active_job:
        payload["job_type"] = active_job.get("job_type")
        payload["group"] = active_job.get("group")
        payload["active_job"] = active_job
    return jsonify(payload)


def _build_data_payload() -> dict:
    from data_store import storage_debug, storage_mode, storage_warning
    from platform_schedule import platform_schedule_status

    s = load_settings()
    accounts = [a.to_public() for a in load_accounts()]
    working, off = partition_enabled_by_attendance()
    info = platform_schedule_status(s)
    last_run = s.last_run if isinstance(s.last_run, dict) else None
    return {
        "ok": True,
        "automation_enabled": s.automation_enabled,
        "base_url_set": bool(s.base_url),
        "base_url": s.base_url,
        "accounts_count": len(accounts),
        "accounts": accounts,
        "schedule": s.schedule,
        "sleep_schedule_enabled": s.sleep_schedule_enabled,
        "platform_schedule": info,
        "mac_schedule": info,
        "server_time": datetime.now(JST).strftime("%H:%M"),
        "last_run": last_run,
        "attendance_date": s.attendance_date,
        "attendance_label": attendance_today_label(),
        "working_today_ids": list(load_working_today_ids()),
        "working_today": [a.to_public() for a in working],
        "off_today": [a.to_public() for a in off],
        "storage_mode": storage_mode(),
        "storage_warning": storage_warning(),
        "storage_debug": storage_debug(),
    }


@app.post("/api/sleep-schedule")
def api_sleep_schedule():
    from platform_schedule import set_sleep_schedule

    enabled = bool((request.json or {}).get("enabled", True))
    settings = set_sleep_schedule(enabled)
    return jsonify(
        {
            "ok": True,
            "sleep_schedule_enabled": settings.sleep_schedule_enabled,
        }
    )


@app.post("/api/mac-schedule/sync")
def api_mac_schedule_sync():
    if sys.platform not in ("darwin", "win32"):
        return jsonify({"ok": False, "error": "Mac / Windows のみ対応"}), 400
    from platform_schedule import sync_platform_schedule

    info = sync_platform_schedule()
    return jsonify({"ok": True, **info})


def main():
    start_scheduler()
    if sys.platform in ("darwin", "win32"):
        try:
            from platform_schedule import sync_platform_schedule

            info = sync_platform_schedule()
            logging.info("OS 定時登録: %s", info.get("message", info))
        except Exception:
            logging.exception("OS 定時登録の同期に失敗")
    port = int(__import__("os").getenv("PORT", "5050"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
