"""1アカウント分のミテネ送信実行."""

from __future__ import annotations

import logging
import os
from typing import Any

import yaml
from dotenv import load_dotenv

from human_behavior import HumanBehavior
from mitene_sender import (
    BUDGET_READ_FAILED_PREFIX,
    DEFAULT_PRIORITY_STEPS,
    AccountStallTimeout,
    BrowserConfig,
    DailyLimitReached,
    LoginConfig,
    MiteneGiftConfig,
    MiteneSender,
    MiteneStandardConfig,
    PriorityStep,
)
from app_paths import auth_root, logs_root
from store import Account, ROOT

logger = logging.getLogger(__name__)


def resolve_sent_this_run(sender: MiteneSender | None) -> int:
    """今回実行で実際に送った件数（MiteneSender インスタンス内のみ参照）."""
    if sender is None:
        return 0
    report = getattr(sender, "_last_run_report", None) or {}
    from_report = int(report.get("sent") or 0)
    if from_report > 0:
        return from_report
    sent_keys = getattr(sender, "_sent_member_keys", None)
    if sent_keys:
        return len(sent_keys)
    return 0


def load_app_config() -> dict:
    path = ROOT / "config.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _as_config_dict(value: Any, name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    logger.warning("%s が dict ではありません: type=%s", name, type(value).__name__)
    return {}


def _parse_priority_steps(standard_raw: dict[str, Any]) -> list[PriorityStep]:
    # NOTE (baseline 2026-07-12 / Phase 1):
    # 戻り値の PriorityStep 列は MiteneStandardConfig.priority_steps に入るが、
    # 現行の送信経路 _execute_phased_send_pipeline は 7 フェーズの順序・条件を
    # ハードコードしており、ここでの解析結果は「priority_steps が非空か（=
    # _send_mitene_standard の `if steps:` を真にするか）」の判定にしか使われない。
    # config.yaml はステップ内容の完全な制御元ではない。詳細は docs/current_baseline.md。
    # （config とパイプラインの統合は将来 Phase の課題。Phase 1 では変更しない）
    steps_raw = standard_raw.get("priority_steps") or []
    if isinstance(steps_raw, dict):
        if steps_raw.get("tab"):
            steps_raw = [steps_raw]
        else:
            logger.warning(
                "priority_steps が dict です（list が必要）。無視します。"
            )
            return []
    if not isinstance(steps_raw, list):
        logger.warning(
            "priority_steps が不正です: type=%s",
            type(steps_raw).__name__,
        )
        return []
    if steps_raw:
        parsed = [
            PriorityStep(
                tab=str(s["tab"]),
                sub_tab=str(s.get("sub_tab", "")),
                condition=str(s.get("condition", "always")),
                member_filter=str(s.get("member_filter", "sendable")),
                list_path=str(s.get("list_path", "")),
                max_members=int(s.get("max_members", 0)),
            )
            for s in steps_raw
            if isinstance(s, dict) and s.get("tab")
        ]
        if parsed:
            return parsed
    return list(DEFAULT_PRIORITY_STEPS)


def build_sender(
    *,
    base_url: str,
    login_id: str,
    password: str,
    account_id: str,
    dry_run: bool = False,
    headed: bool = False,
    progress_callback: Any = None,
) -> MiteneSender:
    load_dotenv(ROOT / ".env")
    cfg = load_app_config()
    browser_raw = _as_config_dict(cfg.get("browser"), "browser")
    if headed:
        browser_raw = {**browser_raw, "headless": False}
    vp = _as_config_dict(browser_raw.get("viewport"), "browser.viewport")
    standard_raw = _as_config_dict(cfg.get("mitene"), "mitene")
    gift_raw = _as_config_dict(cfg.get("mitene_gift"), "mitene_gift")
    login_raw = _as_config_dict(cfg.get("login"), "login")
    human_raw = _as_config_dict(cfg.get("human"), "human")
    logging_raw = _as_config_dict(cfg.get("logging"), "logging")

    log_dir = logs_root() / account_id
    auth_path = auth_root() / f"{account_id}.json"

    return MiteneSender(
        base_url=base_url,
        login_id=login_id,
        password=password,
        flow=cfg.get("flow", "standard"),
        login=LoginConfig(**login_raw),
        standard=MiteneStandardConfig(
            find_members_button=standard_raw.get(
                "find_members_button", "ミテネできる会員を探す"
            ),
            remaining_label=standard_raw.get("remaining_label", "ミテネ残り回数"),
            mitene_history_label=standard_raw.get(
                "mitene_history_label", "ミテネ履歴"
            ),
            priority_steps=_parse_priority_steps(standard_raw),
            max_send_per_run=int(standard_raw.get("max_send_per_run", 0)),
            must_use_full_budget=bool(standard_raw.get("must_use_full_budget", True)),
            max_scroll_rounds=int(standard_raw.get("max_scroll_rounds", 30)),
            member_cooldown_days=int(standard_raw.get("member_cooldown_days", 0)),
            max_no_history_sends_per_day=int(
                standard_raw.get("max_no_history_sends_per_day", 0)
            ),
            confirm_buttons=list(
                standard_raw.get(
                    "confirm_buttons", ["ミテネを送る", "送る", "OK"]
                )
            ),
            skip_special_banners=bool(standard_raw.get("skip_special_banners", True)),
            member_extraction_debug=bool(
                standard_raw.get("member_extraction_debug", False)
            )
            or os.environ.get("MITENE_MEMBER_EXTRACTION_DEBUG", "")
            .strip()
            .lower()
            in ("1", "true", "yes", "on"),
            member_scroll_merge_parse=bool(
                standard_raw.get("member_scroll_merge_parse", False)
            )
            or os.environ.get("MITENE_SCROLL_MERGE_PARSE", "")
            .strip()
            .lower()
            in ("1", "true", "yes", "on"),
        ),
        gift=MiteneGiftConfig(
            menu_button_text=gift_raw.get("menu_button_text", "ミテネギフトを送る"),
            image_index=int(gift_raw.get("image_index", 0)),
            image_alt=str(gift_raw.get("image_alt", "")),
            user_selection=gift_raw.get("user_selection", "unsent_only"),
            message=str(gift_raw.get("message", "")),
        ),
        browser=BrowserConfig(
            headless=bool(browser_raw.get("headless", True)),
            slow_mo_ms=int(browser_raw.get("slow_mo_ms", 150)),
            timeout_ms=int(browser_raw.get("timeout_ms", 45000)),
            viewport_width=int(vp.get("width", 390)),
            viewport_height=int(vp.get("height", 844)),
            is_mobile=bool(vp.get("is_mobile", True)),
        ),
        auth_state_path=auth_path,
        log_dir=log_dir,
        screenshot_on_error=bool(logging_raw.get("screenshot_on_error", True)),
        dry_run=dry_run,
        human=HumanBehavior(human_raw),
        progress_callback=progress_callback,
    )


def _account_error_result(
    account: Account,
    sender: MiteneSender | None,
    error: BaseException,
    *,
    dry_run: bool = False,
) -> dict:
    """送信失敗時の結果（部分送信があれば件数を残す）."""
    result: dict = {
        "account_id": account.id,
        "name": account.name,
        "ok": False,
        "status": "error",
        "error": str(error),
        "dry_run": dry_run,
    }
    if sender is None:
        return result
    try:
        sent = resolve_sent_this_run(sender)
        if sent > 0:
            result["sent"] = sent
            report = getattr(sender, "_last_run_report", None) or {}
            if report:
                result["report"] = report
            result["ok"] = True
            result["status"] = "success"
            result["message"] = str(error)
    except Exception:
        pass
    return result


def run_for_account(
    account: Account,
    base_url: str,
    *,
    dry_run: bool = False,
    headed: bool = False,
    respect_enabled: bool = True,
) -> dict:
    """1人分を実行し結果 dict を返す."""
    if respect_enabled and not account.enabled:
        return {
            "account_id": account.id,
            "name": account.name,
            "skipped": "disabled",
            "status": "skipped",
        }
    if not base_url:
        return {
            "account_id": account.id,
            "name": account.name,
            "error": "base_url未設定",
            "status": "error",
            "ok": False,
        }

    from job_runner import JobCancelled, get_send_progress_callback

    sender: MiteneSender | None = None
    result: dict | None = None
    try:
        sender = build_sender(
            base_url=base_url,
            login_id=account.login_id,
            password=account.password,
            account_id=account.id,
            dry_run=dry_run,
            headed=headed,
            progress_callback=get_send_progress_callback(),
        )
        sent = sender.run()
        report = getattr(sender, "_last_run_report", None) or {}
        hint = str(report.get("status_hint") or "")
        remaining_final = report.get("remaining_final")
        if dry_run:
            status = "dry_run"
        elif sent > 0:
            status = "success"
        elif hint == "completed":
            # 残り回数を 0 と confident に確認できた → 完了扱い（ERROR にしない）
            status = "no_remaining"
        elif hint == "completed_with_remaining":
            # 残り回数 > 0 だが安全な送信候補なし → ERROR にしない
            status = "completed_with_remaining"
        elif hint == "budget_read_failed":
            status = "budget_read_failed"
        else:
            status = "zero_send"
        result = {
            "account_id": account.id,
            "name": account.name,
            "ok": status in (
                "success",
                "no_remaining",
                "completed_with_remaining",
                "dry_run",
            ),
            "sent": sent,
            "dry_run": dry_run,
            "status": status,
        }
        if isinstance(remaining_final, int):
            result["remaining"] = remaining_final
        if sent == 0 and not dry_run:
            result["message"] = sender.zero_send_message()
            if report:
                result["report"] = report
    except DailyLimitReached as e:
        sent = resolve_sent_this_run(sender)
        result = {
            "account_id": account.id,
            "name": account.name,
            "ok": True,
            "sent": sent,
            "status": "no_remaining",
            "message": str(e),
            "dry_run": dry_run,
        }
        if sender is not None and sender._last_run_report:
            result["report"] = sender._last_run_report
    except AccountStallTimeout as e:
        # 10分超＋実質停止 → このアカウントだけ timeout 扱い（batch は継続・§9）。
        sent = resolve_sent_this_run(sender)
        logger.warning("%s: アカウント処理を打ち切り（%s）", account.name, e)
        result = {
            "account_id": account.id,
            "name": account.name,
            "ok": False,
            "sent": sent,
            "status": "timeout",
            "error": f"アカウント処理を打ち切り: {e}",
            "message": f"10分を超え進捗が停止したため打ち切りました（{sent} 件送信済）",
            "dry_run": dry_run,
        }
        if sender is not None and sender._last_run_report:
            result["report"] = sender._last_run_report
    except RuntimeError as e:
        msg = str(e)
        if BUDGET_READ_FAILED_PREFIX in msg:
            result = {
                "account_id": account.id,
                "name": account.name,
                "ok": False,
                "sent": 0,
                "status": "budget_read_failed",
                "error": msg,
                "message": msg,
                "dry_run": dry_run,
            }
        else:
            logger.exception("%s: 送信失敗", account.name)
            result = _account_error_result(account, sender, e, dry_run=dry_run)
    except JobCancelled:
        raise
    except Exception as e:
        logger.exception("%s: 送信失敗", account.name)
        result = _account_error_result(account, sender, e, dry_run=dry_run)
    if result is None:
        result = {
            "account_id": account.id,
            "name": account.name,
            "ok": False,
            "status": "error",
            "error": "送信結果を取得できませんでした",
            "dry_run": dry_run,
        }
    return result
