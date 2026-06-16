"""定時送信の OS 登録で共通利用するロジック."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys

from store import (
    JST,
    ROOT,
    SLOT_KEYS,
    SLOT_WINDOWS,
    WEEKDAYS,
    Settings,
)

RUN_SCRIPT = ROOT / "scripts" / "run_scheduled.py"
MAX_WAKE_EVENTS = 12
WAKE_MINUTES_BEFORE = 3

DAY_TO_WINDOWS: dict[str, str] = {
    "sun": "Sunday",
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
}

DAY_TO_LAUNCHD: dict[str, int] = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


def is_frozen_runtime() -> bool:
    return bool(getattr(sys, "frozen", False))


def python_executable() -> Path:
    if is_frozen_runtime():
        return Path(sys.executable)
    if sys.platform == "win32":
        venv = ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        venv = ROOT / ".venv" / "bin" / "python"
    if venv.exists():
        return venv
    return Path(sys.executable)


def scheduled_runner_argv() -> list[str]:
    """OS 定時実行用のコマンドライン引数."""
    if is_frozen_runtime():
        return [str(python_executable()), "--scheduled"]
    return [str(python_executable()), str(RUN_SCRIPT)]


def has_enabled_slots(settings: Settings) -> bool:
    for day in WEEKDAYS:
        for slot in SLOT_KEYS:
            if settings.schedule.get(day, {}).get(slot):
                return True
    return False


def build_weekly_triggers(settings: Settings) -> list[tuple[str, int, int]]:
    """(曜日名, 時, 分) — 自動送信ON・スリープ対策ON の枠のみ."""
    if not settings.automation_enabled or not settings.sleep_schedule_enabled:
        return []
    triggers: list[tuple[str, int, int]] = []
    for day in WEEKDAYS:
        for slot in SLOT_KEYS:
            if not settings.schedule.get(day, {}).get(slot):
                continue
            hour, _ = SLOT_WINDOWS[slot]
            triggers.append((day, hour, 1))
    return triggers


def build_launchd_intervals(settings: Settings) -> list[dict[str, int]]:
    triggers = build_weekly_triggers(settings)
    return [
        {"Weekday": DAY_TO_LAUNCHD[day], "Hour": hour, "Minute": minute}
        for day, hour, minute in triggers
    ]


def iter_upcoming_wake_times(settings: Settings, days: int = 10) -> list[datetime]:
    if not settings.automation_enabled or not settings.sleep_schedule_enabled:
        return []
    now = datetime.now(JST)
    wakes: list[datetime] = []
    for offset in range(days):
        day = now.date() + timedelta(days=offset)
        weekday = WEEKDAYS[day.weekday()]
        for slot in SLOT_KEYS:
            if not settings.schedule.get(weekday, {}).get(slot):
                continue
            hour, _ = SLOT_WINDOWS[slot]
            target = datetime(
                day.year, day.month, day.day, hour, 0, 0, tzinfo=JST
            ) - timedelta(minutes=WAKE_MINUTES_BEFORE)
            if target > now:
                wakes.append(target)
    wakes.sort()
    return wakes[:MAX_WAKE_EVENTS]
