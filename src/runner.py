"""1アカウント分のミテネ送信実行."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from human_behavior import HumanBehavior
from mitene_sender import (
    DEFAULT_PRIORITY_STEPS,
    BrowserConfig,
    DailyLimitReached,
    LoginConfig,
    MiteneGiftConfig,
    MiteneSender,
    MiteneStandardConfig,
    PriorityStep,
)
from store import Account, ROOT

logger = logging.getLogger(__name__)


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

    log_dir = ROOT / "logs" / account_id
    auth_path = ROOT / "playwright" / ".auth" / f"{account_id}.json"

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
    )


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

    sender = build_sender(
        base_url=base_url,
        login_id=account.login_id,
        password=account.password,
        account_id=account.id,
        dry_run=dry_run,
        headed=headed,
    )
    try:
        sent = sender.run()
        status = "dry_run" if dry_run else ("success" if sent > 0 else "zero_send")
        result: dict = {
            "account_id": account.id,
            "name": account.name,
            "ok": True,
            "sent": sent,
            "dry_run": dry_run,
            "status": status,
        }
        if sent == 0 and not dry_run:
            result["message"] = sender.zero_send_message()
            if sender._last_run_report:
                result["report"] = sender._last_run_report
        return result
    except DailyLimitReached as e:
        return {
            "account_id": account.id,
            "name": account.name,
            "ok": True,
            "sent": 0,
            "status": "no_remaining",
            "message": str(e),
        }
    except Exception as e:
        logger.exception("%s: 送信失敗", account.name)
        return {
            "account_id": account.id,
            "name": account.name,
            "ok": False,
            "status": "error",
            "error": str(e),
        }
