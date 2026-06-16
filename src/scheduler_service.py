"""定時送信スケジューラ."""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

import yaml

from human_behavior import HumanBehavior
from runner import ROOT, run_for_account
from store import (
    JST,
    load_accounts,
    load_settings,
    run_slot_id,
    save_last_run_report,
    save_settings,
    is_scheduled_now,
)

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None
_run_lock = threading.Lock()
_scheduled_run_depth = 0


def run_all_scheduled(
    force: bool = False,
    dry_run: bool = False,
    require_automation: bool = True,
) -> list[dict]:
    global _scheduled_run_depth
    with _run_lock:
        _scheduled_run_depth += 1
    try:
        return _run_all_scheduled_impl(force=force, dry_run=dry_run, require_automation=require_automation)
    finally:
        with _run_lock:
            _scheduled_run_depth -= 1


def is_scheduled_run_in_progress() -> bool:
    with _run_lock:
        return _scheduled_run_depth > 0


def _run_all_scheduled_impl(
    force: bool = False,
    dry_run: bool = False,
    require_automation: bool = True,
) -> list[dict]:
    settings = load_settings()
    now = datetime.now(JST)
    slot_id = run_slot_id(now)

    if require_automation and not settings.automation_enabled:
        return [{"error": "自動送信がOFFです"}]

    if not force:
        if not is_scheduled_now(settings, now):
            return []
        if settings.last_run_slot == slot_id:
            logger.info("このスロットは実行済み: %s", slot_id)
            return []

    if not settings.base_url:
        logger.error("ログインURL（base_url）が未設定です")
        return [{"error": "base_url未設定"}]

    human_cfg = {}
    cfg_path = ROOT / "config.yaml"
    if cfg_path.exists():
        with cfg_path.open(encoding="utf-8") as f:
            human_cfg = yaml.safe_load(f).get("human", {})
    human = HumanBehavior(human_cfg)

    accounts = [a for a in load_accounts() if a.enabled]
    if human.shuffle_account_order and len(accounts) > 1:
        random.shuffle(accounts)

    results = []
    for i, account in enumerate(accounts):
        if i > 0 and not dry_run:
            human.between_accounts_pause()
        logger.info("送信開始: %s (%s)", account.name, "dry-run" if dry_run else "本番")
        results.append(
            run_for_account(account, settings.base_url, dry_run=dry_run)
        )

    if not force and not dry_run and settings.automation_enabled:
        settings.last_run_slot = slot_id
        save_settings(settings)

    if results:
        save_last_run_report("scheduled", results, dry_run=dry_run)

    return results


def tick() -> None:
    try:
        results = run_all_scheduled()
        if results:
            logger.info("スケジュール実行完了: %d 件", len(results))
    except Exception:
        logger.exception("スケジュール実行エラー")


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone=str(JST))
    _scheduler.add_job(tick, "cron", minute="*/1", id="mitene_tick")
    _scheduler.start()
    logger.info(
        "スケジューラ起動（毎分チェック・9時/0時/19時台に送信）"
    )
    return _scheduler
