"""macOS: launchd + pmset."""

from __future__ import annotations

import logging
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from store import ROOT, Settings, load_settings

from .common import (
    RUN_SCRIPT,
    build_launchd_intervals,
    has_enabled_slots,
    iter_upcoming_wake_times,
    python_executable,
)

logger = logging.getLogger(__name__)

LABEL = "com.local.mitene.sender"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _launchctl(cmd: str, plist: Path) -> tuple[bool, str]:
    uid = subprocess.run(
        ["id", "-u"], capture_output=True, text=True, check=True
    ).stdout.strip()
    target = f"gui/{uid}"
    if cmd == "unload":
        for args in (
            ["launchctl", "bootout", target, str(plist)],
            ["launchctl", "unload", str(plist)],
        ):
            r = subprocess.run(args, capture_output=True, text=True)
            if r.returncode == 0:
                return True, ""
        return False, "launchctl bootout/unload failed"
    for args in (
        ["launchctl", "bootstrap", target, str(plist)],
        ["launchctl", "load", str(plist)],
    ):
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode == 0:
            return True, ""
    err = (r.stderr or r.stdout or "launchctl failed").strip()
    return False, err


def uninstall_launch_agent() -> None:
    if PLIST_PATH.exists():
        _launchctl("unload", PLIST_PATH)
        PLIST_PATH.unlink(missing_ok=True)


def install_launch_agent(intervals: list[dict[str, int]]) -> tuple[bool, str]:
    if not intervals:
        uninstall_launch_agent()
        return True, ""

    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    python = python_executable()
    if not RUN_SCRIPT.exists():
        return False, f"スクリプトがありません: {RUN_SCRIPT}"

    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(RUN_SCRIPT)],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"PYTHONPATH": str(ROOT / "src")},
        "StartCalendarInterval": intervals,
        "StandardOutPath": str(ROOT / "logs" / "launchd.stdout.log"),
        "StandardErrorPath": str(ROOT / "logs" / "launchd.stderr.log"),
        "RunAtLoad": False,
    }
    with PLIST_PATH.open("wb") as f:
        plistlib.dump(plist, f)

    if PLIST_PATH.exists():
        _launchctl("unload", PLIST_PATH)
    return _launchctl("load", PLIST_PATH)


def schedule_wake_events(settings: Settings) -> tuple[int, str | None]:
    wakes = iter_upcoming_wake_times(settings)
    if not wakes:
        return 0, None

    scheduled = 0
    last_err: str | None = None
    for wake_at in wakes:
        stamp = wake_at.strftime("%m/%d/%y %H:%M:%S")
        r = subprocess.run(
            ["pmset", "schedule", "wakeorpoweron", stamp],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            scheduled += 1
        else:
            last_err = (r.stderr or r.stdout or "pmset failed").strip()
            logger.warning("pmset wake 失敗 %s: %s", stamp, last_err)
    return scheduled, last_err


def sync_darwin_schedule(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    intervals = build_launchd_intervals(settings)

    if not settings.sleep_schedule_enabled:
        uninstall_launch_agent()
        return _result(
            settings,
            installed=False,
            message="スリープ対策はOFFです",
        )

    if not settings.automation_enabled or not has_enabled_slots(settings):
        uninstall_launch_agent()
        return _result(
            settings,
            installed=False,
            message="自動送信ONかつ送信時間を1つ以上ONにすると登録されます",
        )

    ok, err = install_launch_agent(intervals)
    wake_count, wake_err = schedule_wake_events(settings)

    wake_hint = ""
    if wake_count == 0 and wake_err and "root" in wake_err.lower():
        wake_hint = (
            " スリープ解除は管理者権限が必要:"
            " bash scripts/schedule_wake_sudo.sh を実行するか、"
            "システム設定→バッテリー→スケジュールで起動時刻を設定。"
        )

    return _result(
        settings,
        installed=ok,
        intervals=len(intervals),
        wake_scheduled=wake_count,
        wake_needs_sudo=bool(wake_err and "root" in (wake_err or "").lower()),
        error=err or wake_err,
        message=(
            f"【Mac】定時実行 {len(intervals)}枠を登録。"
            f"スリープ解除予約 {wake_count}件。"
            "電源接続・フタ開きが確実。"
            + wake_hint
        ),
        plist_path=str(PLIST_PATH),
    )


def _result(settings: Settings, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "platform": "darwin",
        "supported": True,
        "sleep_schedule_enabled": settings.sleep_schedule_enabled,
    }
    base.update(extra)
    return base
