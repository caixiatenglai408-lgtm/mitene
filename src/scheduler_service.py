"""手動送信ジョブ（管理画面の「送信開始」から呼ばれる。時間指定の定時実行は廃止）."""

from __future__ import annotations

import logging
from typing import Any

import yaml

from human_behavior import HumanBehavior
from runner import ROOT, run_for_account
from store import (
    accounts_for_manual_group,
    get_account,
    load_settings,
    save_last_run_report,
)

logger = logging.getLogger(__name__)


def _safe_report_job_progress(
    job_id: str | None,
    *,
    account_name: str = "",
    account_id: str = "",
    partial_results: list[dict] | None,
    dry_run: bool,
) -> None:
    if not job_id or partial_results is None:
        return
    try:
        from job_runner import report_job_progress

        report_job_progress(
            job_id,
            account_name=account_name,
            account_id=account_id,
            partial_results=partial_results,
            dry_run=dry_run,
        )
    except Exception:
        logger.warning("ジョブ進捗の更新に失敗しました", exc_info=True)


def run_send_job(
    *,
    group: str = "all",
    dry_run: bool = False,
    job_id: str | None = None,
) -> list[dict]:
    """手動送信ジョブ（1人失敗しても全員まで続行・必ず結果リストを返す）."""
    from job_runner import JobCancelled, bind_send_progress, clear_send_progress, wait_if_paused

    settings = load_settings()
    if not settings.base_url:
        logger.error("ログインURL（base_url）が未設定です")
        return [{"error": "base_url未設定"}]

    human_cfg = {}
    cfg_path = ROOT / "config.yaml"
    if cfg_path.exists():
        with cfg_path.open(encoding="utf-8") as f:
            human_cfg = yaml.safe_load(f).get("human", {})
    human = HumanBehavior(human_cfg)

    accounts = accounts_for_manual_group(group)
    if not accounts:
        if group == "working":
            return [{"error": "本日出勤の女の子がいません。登録一覧でチェックしてください。"}]
        if group == "off":
            return [{"error": "お休みの女の子がいません。"}]
        return [{"error": "送信対象の女の子がいません。"}]

    results: list[dict] = []
    cancelled = False
    last_account_name = ""
    last_account_id = ""

    try:
        for i, account in enumerate(accounts):
            last_account_name = account.name
            last_account_id = account.id

            try:
                wait_if_paused(job_id)
            except JobCancelled:
                cancelled = True
                break

            bind_send_progress(job_id, account.id, account.name)
            try:
                if i > 0 and not dry_run:
                    human.between_accounts_pause()
                logger.info(
                    "送信開始: %s (%s) [%d/%d]",
                    account.name,
                    "dry-run" if dry_run else "本番",
                    i + 1,
                    len(accounts),
                )
                try:
                    result = run_for_account(
                        account, settings.base_url, dry_run=dry_run
                    )
                except JobCancelled:
                    cancelled = True
                    break
                except Exception as e:
                    logger.exception("%s: 送信失敗（続行）", account.name)
                    result = {
                        "account_id": account.id,
                        "name": account.name,
                        "ok": False,
                        "status": "error",
                        "error": str(e),
                        "dry_run": dry_run,
                    }
                if dry_run:
                    result["dry_run"] = True
                results.append(result)
            except Exception as e:
                logger.exception("%s: 送信処理の異常（続行）", account.name)
                results.append(
                    {
                        "account_id": account.id,
                        "name": account.name,
                        "ok": False,
                        "status": "error",
                        "error": str(e),
                        "dry_run": dry_run,
                    }
                )
            finally:
                clear_send_progress()

            _safe_report_job_progress(
                job_id,
                account_name=account.name,
                account_id=account.id,
                partial_results=results,
                dry_run=dry_run,
            )
    finally:
        _safe_report_job_progress(
            job_id,
            account_name=last_account_name,
            account_id=last_account_id,
            partial_results=results,
            dry_run=dry_run,
        )

    if cancelled:
        logger.info("手動送信ジョブを中止しました（%d/%d 名まで処理）", len(results), len(accounts))

    if results:
        try:
            save_last_run_report("manual", results, dry_run=dry_run)
        except Exception:
            logger.warning("送信結果の保存に失敗しました", exc_info=True)
    return results


def run_manual_batch(
    *,
    group: str = "all",
    dry_run: bool = False,
    job_id: str | None = None,
) -> list[dict]:
    """手動送信（全員 / 本日出勤のみ / お休みのみ）."""
    return run_send_job(group=group, dry_run=dry_run, job_id=job_id)


def run_single_account(
    account_id: str,
    *,
    dry_run: bool = False,
    respect_enabled: bool = False,
    job_id: str | None = None,
) -> list[dict]:
    """1人分の手動送信（進捗・一時停止・中止に対応）."""
    from job_runner import (
        JobCancelled,
        bind_send_progress,
        clear_send_progress,
        wait_if_paused,
    )

    settings = load_settings()
    account = get_account(account_id)
    if not account:
        return [{"error": "アカウントが見つかりません"}]
    if not settings.base_url:
        return [{"error": "base_url未設定"}]

    result: dict[str, Any] = {
        "account_id": account.id,
        "name": account.name,
        "ok": False,
        "status": "error",
        "error": "送信結果を取得できませんでした",
        "dry_run": dry_run,
    }

    try:
        try:
            wait_if_paused(job_id)
        except JobCancelled:
            raise

        bind_send_progress(job_id, account.id, account.name)
        try:
            logger.info(
                "送信開始: %s (%s)",
                account.name,
                "dry-run" if dry_run else "本番",
            )
            result = run_for_account(
                account,
                settings.base_url,
                dry_run=dry_run,
                respect_enabled=respect_enabled,
            )
        except JobCancelled:
            raise
        except Exception as e:
            logger.exception("%s: 送信失敗", account.name)
            from runner import _account_error_result

            result = _account_error_result(account, None, e, dry_run=dry_run)
        finally:
            clear_send_progress()
    finally:
        if dry_run:
            result["dry_run"] = True
        _safe_report_job_progress(
            job_id,
            account_name=account.name,
            account_id=account.id,
            partial_results=[result],
            dry_run=dry_run,
        )

    return [result]
