"""OS 定時登録（Mac / Windows）."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from store import Settings, load_settings, save_settings

from .common import build_weekly_triggers, has_enabled_slots, iter_upcoming_wake_times

__all__ = [
    "sync_platform_schedule",
    "set_sleep_schedule",
    "platform_schedule_status",
    "iter_upcoming_wake_times",
]


def platform_schedule_status(settings: Settings | None = None) -> dict[str, Any]:
    """登録状態の参照のみ（launchd / タスクの再同期はしない）."""
    settings = settings or load_settings()

    if sys.platform == "darwin":
        from .darwin import PLIST_PATH, build_launchd_intervals

        intervals = build_launchd_intervals(settings)
        if not settings.sleep_schedule_enabled:
            return {
                "platform": "darwin",
                "supported": True,
                "installed": False,
                "message": "スリープ対策はOFFです",
            }
        if not settings.automation_enabled or not intervals:
            return {
                "platform": "darwin",
                "supported": True,
                "installed": False,
                "message": "自動送信ONかつ送信時間を1つ以上ONにすると登録されます",
            }
        installed = PLIST_PATH.exists()
        return {
            "platform": "darwin",
            "supported": True,
            "installed": installed,
            "intervals": len(intervals),
            "message": (
                f"【Mac】定時実行 {len(intervals)}枠"
                + ("登録済み" if installed else "未登録・再同期してください")
            ),
        }

    if sys.platform == "win32":
        triggers = build_weekly_triggers(settings)
        if not settings.sleep_schedule_enabled:
            return {
                "platform": "windows",
                "supported": True,
                "installed": False,
                "message": "スリープ対策はOFFです",
            }
        if not settings.automation_enabled or not triggers:
            return {
                "platform": "windows",
                "supported": True,
                "installed": False,
                "message": "自動送信ONかつ送信時間を1つ以上ONにすると登録されます",
            }
        return {
            "platform": "windows",
            "supported": True,
            "installed": True,
            "intervals": len(triggers),
            "message": f"【Windows】タスク MiteneAutoSend（{len(triggers)}枠）",
        }

    return {
        "platform": sys.platform,
        "supported": False,
        "message": "Mac / Windows のみスリープ対策に対応しています",
    }


def sync_platform_schedule(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()

    if sys.platform == "darwin":
        from .darwin import sync_darwin_schedule

        return sync_darwin_schedule(settings)
    if sys.platform == "win32":
        from .windows import sync_windows_schedule

        return sync_windows_schedule(settings)

    return {
        "platform": sys.platform,
        "supported": False,
        "message": "Mac / Windows のみスリープ対策に対応しています",
    }


def set_sleep_schedule(enabled: bool) -> Settings:
    settings = load_settings()
    settings.sleep_schedule_enabled = enabled
    save_settings(settings)
    return settings
