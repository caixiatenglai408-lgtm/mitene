"""送信ワーカー API — Playwright 実行専用（Railway / Render 等にデプロイ）."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

load_dotenv(ROOT / ".env")

from app_paths import setup_runtime  # noqa: E402

setup_runtime()

from job_runner import (  # noqa: E402
    cancel_job,
    get_job,
    has_running_jobs,
    is_system_busy,
    pause_job,
    resume_job,
    start_background_job,
    validate_before_run,
)
from runner import run_for_account  # noqa: E402
from scheduler_service import run_manual_batch, run_single_account  # noqa: E402
from store import get_account, load_settings  # noqa: E402

app = Flask(__name__)


def _auth_ok() -> bool:
    secret = (os.getenv("MITENE_WORKER_SECRET") or "").strip()
    if not secret:
        return True
    return request.headers.get("X-Worker-Secret", "") == secret


def _unauthorized():
    return jsonify({"ok": False, "error": "認証に失敗しました"}), 401


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "mitene-worker"})


@app.get("/system/busy")
def api_system_busy():
    if not _auth_ok():
        return _unauthorized()
    busy = is_system_busy()
    return jsonify({"ok": True, "busy": busy, "has_running_jobs": has_running_jobs()})


@app.post("/run/account/<account_id>")
def api_run_account(account_id: str):
    if not _auth_ok():
        return _unauthorized()
    settings = load_settings()
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "error": "アカウントが見つかりません"}), 404
    if not settings.base_url:
        return jsonify({"ok": False, "error": "ログインURLを設定してください"}), 400
    payload = request.get_json(silent=True) or {}
    result = run_for_account(
        account,
        settings.base_url,
        dry_run=bool(payload.get("dry_run", False)),
        headed=False,
        respect_enabled=bool(payload.get("respect_enabled", True)),
    )
    return jsonify({"ok": True, "result": result})


@app.post("/jobs/run-account/<account_id>")
def api_jobs_run_account(account_id: str):
    if not _auth_ok():
        return _unauthorized()
    settings = load_settings()
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "error": "アカウントが見つかりません"}), 404
    if not settings.base_url:
        return jsonify({"ok": False, "error": "ログインURLを設定してください"}), 400
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    respect_enabled = bool(payload.get("respect_enabled", True))
    job_id = start_background_job(
        f"「{account.name}」を送信",
        lambda jid, aid=account_id, d=dry_run, r=respect_enabled: run_single_account(
            aid, dry_run=d, respect_enabled=r, job_id=jid
        ),
        dry_run=dry_run,
        job_type="single",
        account_name=account.name,
    )
    return jsonify({"ok": True, "job_id": job_id})


@app.post("/jobs/run-all")
def api_jobs_run_all():
    if not _auth_ok():
        return _unauthorized()
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    group = str(payload.get("group") or "all")
    if group not in ("all", "working", "off"):
        return jsonify({"ok": False, "error": "group は all / working / off です"}), 400
    err = validate_before_run(group)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    name = str(payload.get("name") or ("全員ドライラン" if dry_run else "今すぐ全員送信"))
    job_id = start_background_job(
        name,
        lambda jid, g=group, d=dry_run: run_manual_batch(
            group=g, dry_run=d, job_id=jid
        ),
        dry_run=dry_run,
        job_type="batch",
        group=group,
    )
    return jsonify({"ok": True, "job_id": job_id})


@app.post("/jobs/<job_id>/pause")
def api_job_pause(job_id: str):
    if not _auth_ok():
        return _unauthorized()
    if not pause_job(job_id):
        return jsonify({"ok": False, "error": "一時停止できません"}), 400
    return jsonify({"ok": True, "paused": True})


@app.post("/jobs/<job_id>/resume")
def api_job_resume(job_id: str):
    if not _auth_ok():
        return _unauthorized()
    if not resume_job(job_id):
        return jsonify({"ok": False, "error": "再開できません"}), 400
    return jsonify({"ok": True, "paused": False})


@app.post("/jobs/<job_id>/cancel")
def api_job_cancel(job_id: str):
    if not _auth_ok():
        return _unauthorized()
    if not cancel_job(job_id):
        return jsonify({"ok": False, "error": "中止できません"}), 400
    return jsonify({"ok": True, "cancelled": True})


@app.get("/jobs/<job_id>")
def api_job_status(job_id: str):
    if not _auth_ok():
        return _unauthorized()
    job = get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "ジョブが見つかりません"}), 404
    return jsonify({"ok": True, "job": job})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
