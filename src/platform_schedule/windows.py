"""Windows: タスクスケジューラ（スリープからの復帰 WakeToRun）."""

from __future__ import annotations

import logging
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from store import ROOT, Settings, load_settings

from .common import (
    DAY_TO_WINDOWS,
    RUN_SCRIPT,
    build_weekly_triggers,
    has_enabled_slots,
    is_frozen_runtime,
    python_executable,
    scheduled_runner_argv,
)

logger = logging.getLogger(__name__)

TASK_NAME = "MiteneAutoSend"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def uninstall_scheduled_task() -> tuple[bool, str]:
    script = textwrap.dedent(
        f"""
        $t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
        if ($t) {{ Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false }}
        """
    ).strip()
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "Unregister failed").strip()
    return True, ""


def install_scheduled_task(triggers: list[tuple[str, int, int]]) -> tuple[bool, str]:
    if not triggers:
        return uninstall_scheduled_task()

    argv = scheduled_runner_argv()
    python = Path(argv[0])
    if not python.exists():
        return False, f"実行ファイルが見つかりません: {python}"
    if not is_frozen_runtime() and not RUN_SCRIPT.exists():
        return False, f"スクリプトがありません: {RUN_SCRIPT}"
    task_argument = " ".join(argv[1:]) if len(argv) > 1 else ""

    uninstall_scheduled_task()

    trigger_lines = []
    for day, hour, minute in triggers:
        win_day = DAY_TO_WINDOWS[day]
        at = f"{hour:02d}:{minute:02d}"
        trigger_lines.append(
            f"$Triggers += New-ScheduledTaskTrigger -Weekly "
            f"-DaysOfWeek {win_day} -At {_ps_quote(at)}"
        )

    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out_log = logs / "task_scheduler.stdout.log"
    err_log = logs / "task_scheduler.stderr.log"

    script = textwrap.dedent(
        f"""
        $ErrorActionPreference = 'Stop'
        $Triggers = @()
        {chr(10).join(trigger_lines)}
        $Action = New-ScheduledTaskAction `
          -Execute {_ps_quote(str(python))} `
          -Argument {_ps_quote(task_argument)} `
          -WorkingDirectory {_ps_quote(str(ROOT))}
        $Settings = New-ScheduledTaskSettingsSet `
          -WakeToRun `
          -StartWhenAvailable `
          -AllowStartIfOnBatteries `
          -DontStopIfGoingOnBatteries `
          -ExecutionTimeLimit (New-TimeSpan -Hours 2)
        Register-ScheduledTask `
          -TaskName '{TASK_NAME}' `
          -Action $Action `
          -Trigger $Triggers `
          -Settings $Settings `
          -Force | Out-Null
        """
    ).strip()

    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "Register-ScheduledTask failed").strip()
        logger.error("Windows タスク登録失敗: %s", err)
        return False, err
    return True, ""


def sync_windows_schedule(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    triggers = build_weekly_triggers(settings)

    if not settings.sleep_schedule_enabled:
        uninstall_scheduled_task()
        return _result(
            settings,
            installed=False,
            message="スリープ対策はOFFです",
        )

    if not settings.automation_enabled or not has_enabled_slots(settings):
        uninstall_scheduled_task()
        return _result(
            settings,
            installed=False,
            message="自動送信ONかつ送信時間を1つ以上ONにすると登録されます",
        )

    ok, err = install_scheduled_task(triggers)

    return _result(
        settings,
        installed=ok,
        intervals=len(triggers),
        wake_enabled=True,
        task_name=TASK_NAME,
        error=err or None,
        message=(
            f"【Windows】タスク「{TASK_NAME}」に {len(triggers)}枠を登録。"
            "スリープから復帰して実行（WakeToRun）、"
            "ノートPCは電源接続・フタ開きが確実。"
            "タスクスケジューラで確認できます。"
        ),
    )


def _result(settings: Settings, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "platform": "windows",
        "supported": True,
        "sleep_schedule_enabled": settings.sleep_schedule_enabled,
    }
    base.update(extra)
    return base
