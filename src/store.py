"""設定・女の子アカウントの永続化."""

from __future__ import annotations

import json
import logging
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app_paths import APP_ROOT, DATA_ROOT
from crypto_util import decrypt_secret, encrypt_secret
from data_store import read_accounts as _read_accounts_payload
from data_store import read_settings as _read_settings_payload
from data_store import write_accounts as _write_accounts_payload
from data_store import write_settings as _write_settings_payload

ROOT = APP_ROOT
DATA_DIR = DATA_ROOT / "data"
ACCOUNTS_PATH = DATA_DIR / "accounts.json"
SETTINGS_PATH = DATA_DIR / "settings.json"

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_LABELS = {
    "mon": "月",
    "tue": "火",
    "wed": "水",
    "thu": "木",
    "fri": "金",
    "sat": "土",
    "sun": "日",
}
SLOTS = {
    "09:00": "午前9時",
    "19:00": "午後7時",
    "00:00": "午前0時",
}
# 表示・実行順（辞書の並びと同じ）
SLOT_KEYS = list(SLOTS.keys())
# 各枠の実行可能時間（時, 分未満）— 手動風待機のため30分間
SLOT_WINDOWS: dict[str, tuple[int, int]] = {
    "09:00": (9, 30),
    "00:00": (0, 30),
    "19:00": (19, 30),
}
# 旧設定のキー → 新キー（移行用）
_LEGACY_SLOT_MAP = {"12:00": "00:00"}
JST = ZoneInfo("Asia/Tokyo")


def default_schedule() -> dict[str, dict[str, bool]]:
    empty_day = {slot: False for slot in SLOT_KEYS}
    return {day: dict(empty_day) for day in WEEKDAYS}


@dataclass
class Account:
    id: str
    name: str
    login_id: str
    password: str
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "login_id": self.login_id,
            "enabled": self.enabled,
            "has_password": bool(self.password),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Settings:
    automation_enabled: bool = False
    base_url: str = ""
    schedule: dict[str, dict[str, bool]] = field(default_factory=default_schedule)
    last_run_slot: str = ""
    # macOS: スリープ中も launchd で定時送信（管理画面が閉じていても可）
    sleep_schedule_enabled: bool = True
    # 直近の送信結果（手動・自動共通の表示用）
    last_run: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "automation_enabled": self.automation_enabled,
            "base_url": self.base_url,
            "schedule": self.schedule,
            "last_run_slot": self.last_run_slot,
            "sleep_schedule_enabled": self.sleep_schedule_enabled,
        }
        if self.last_run:
            d["last_run"] = self.last_run
        return d


def _now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_schedule(schedule: dict) -> dict[str, dict[str, bool]]:
    """全曜日・全時間枠が揃うよう補完（旧12:00→00:00へ移行）."""
    normalized = default_schedule()
    for day in WEEKDAYS:
        day_data = dict(schedule.get(day) or {})
        for old_key, new_key in _LEGACY_SLOT_MAP.items():
            if old_key in day_data and new_key not in day_data:
                day_data[new_key] = day_data[old_key]
        for slot in SLOT_KEYS:
            if slot in day_data:
                normalized[day][slot] = bool(day_data[slot])
        # 同一曜日は1枠のみ（先頭のONを残す）
        enabled = [s for s in SLOT_KEYS if normalized[day][s]]
        if len(enabled) > 1:
            keep = enabled[0]
            for slot in SLOT_KEYS:
                normalized[day][slot] = slot == keep
    return normalized


def load_settings() -> Settings:
    _ensure_data_dir()
    raw = _read_settings_payload(SETTINGS_PATH)
    if raw is None:
        return Settings()
    schedule = _normalize_schedule(raw.get("schedule") or {})
    last_run = raw.get("last_run")
    settings = Settings(
        automation_enabled=bool(raw.get("automation_enabled", False)),
        base_url=str(raw.get("base_url", "")),
        schedule=schedule,
        last_run_slot=str(raw.get("last_run_slot", "")),
        sleep_schedule_enabled=bool(raw.get("sleep_schedule_enabled", True)),
        last_run=last_run if isinstance(last_run, dict) else None,
    )
    # 時間枠の追加・削除・移行時は settings.json を更新
    merged_raw = raw.get("schedule") or {}
    legacy_keys = set(_LEGACY_SLOT_MAP) | {"18:00"}
    needs_save = any(
        slot not in (merged_raw.get(day) or {})
        for day in WEEKDAYS
        for slot in SLOT_KEYS
    ) or any(
        k in (merged_raw.get(day) or {})
        for day in WEEKDAYS
        for k in legacy_keys
    ) or any(
        sum(1 for s in SLOT_KEYS if (merged_raw.get(day) or {}).get(s))
        > 1
        for day in WEEKDAYS
    )
    if needs_save:
        save_settings(settings)
    return settings


def slots_for_template() -> list[tuple[str, str]]:
    """テンプレート用（表示順固定）."""
    return [(key, SLOTS[key]) for key in SLOT_KEYS]


def save_last_run_report(
    source: str,
    results: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """手動・自動送信の結果を settings に保存（管理画面表示用）."""
    from result_display import build_run_display

    settings = load_settings()
    display = build_run_display(results, dry_run=dry_run)
    settings.last_run = {
        "at": _now_iso(),
        "source": source,
        "dry_run": dry_run,
        "display": display,
    }
    save_settings(settings)
    return display


def save_settings(settings: Settings) -> None:
    import sys

    _ensure_data_dir()
    _write_settings_payload(
        SETTINGS_PATH,
        settings.to_dict(),
    )
    if sys.platform in ("darwin", "win32"):
        try:
            from platform_schedule import sync_platform_schedule

            sync_platform_schedule(settings)
        except Exception:
            logging.getLogger(__name__).exception("OS 定時登録の同期に失敗")


def load_accounts() -> list[Account]:
    _ensure_data_dir()
    raw = _read_accounts_payload(ACCOUNTS_PATH)
    if raw is None:
        return []
    accounts = []
    for row in raw.get("accounts", []):
        accounts.append(
            Account(
                id=row["id"],
                name=row.get("name", ""),
                login_id=row.get("login_id", ""),
                password=decrypt_secret(row.get("password", "")),
                enabled=bool(row.get("enabled", True)),
                created_at=row.get("created_at", ""),
                updated_at=row.get("updated_at", ""),
            )
        )
    return accounts


def save_accounts(accounts: list[Account]) -> None:
    _ensure_data_dir()
    payload = {
        "accounts": [
            {
                **{k: v for k, v in asdict(a).items() if k != "password"},
                "password": encrypt_secret(a.password),
            }
            for a in accounts
        ]
    }
    _write_accounts_payload(ACCOUNTS_PATH, payload)


def get_account(account_id: str) -> Account | None:
    for a in load_accounts():
        if a.id == account_id:
            return a
    return None


def normalize_display_name(name: str) -> str:
    return name.strip().replace("\u3000", " ").strip()


def has_duplicate_name(name: str, exclude_id: str | None = None) -> bool:
    """登録一覧に同じ表示名があるか（更新時は自分自身を除く）."""
    key = normalize_display_name(name)
    if not key:
        return False
    for a in load_accounts():
        if exclude_id and a.id == exclude_id:
            continue
        if normalize_display_name(a.name) == key:
            return True
    return False


def upsert_account(
    name: str,
    login_id: str,
    password: str,
    account_id: str | None = None,
    enabled: bool = True,
) -> Account:
    accounts = load_accounts()
    now = _now_iso()
    if account_id:
        for i, a in enumerate(accounts):
            if a.id == account_id:
                accounts[i] = Account(
                    id=a.id,
                    name=name.strip(),
                    login_id=login_id.strip(),
                    password=password if password else a.password,
                    enabled=enabled,
                    created_at=a.created_at,
                    updated_at=now,
                )
                save_accounts(accounts)
                return accounts[i]
        raise ValueError("アカウントが見つかりません")

    account = Account(
        id=str(uuid.uuid4()),
        name=name.strip(),
        login_id=login_id.strip(),
        password=password,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )
    accounts.append(account)
    save_accounts(accounts)
    return account


def set_account_enabled(account_id: str, enabled: bool) -> Account:
    accounts = load_accounts()
    for i, a in enumerate(accounts):
        if a.id == account_id:
            accounts[i] = Account(
                id=a.id,
                name=a.name,
                login_id=a.login_id,
                password=a.password,
                enabled=enabled,
                created_at=a.created_at,
                updated_at=_now_iso(),
            )
            save_accounts(accounts)
            return accounts[i]
    raise ValueError("アカウントが見つかりません")


def delete_account(account_id: str) -> bool:
    accounts = load_accounts()
    new_list = [a for a in accounts if a.id != account_id]
    if len(new_list) == len(accounts):
        return False
    save_accounts(new_list)
    auth_file = ROOT / "playwright" / ".auth" / f"{account_id}.json"
    if auth_file.exists():
        auth_file.unlink()
    return True


def toggle_schedule(day: str, slot: str) -> Settings:
    if day not in WEEKDAYS or slot not in SLOTS:
        raise ValueError("不正な曜日または時間です")
    settings = load_settings()
    settings.schedule = deepcopy(settings.schedule)
    turning_on = not settings.schedule[day][slot]
    if turning_on:
        for s in SLOT_KEYS:
            settings.schedule[day][s] = s == slot
    else:
        settings.schedule[day][slot] = False
    save_settings(settings)
    return settings


def set_automation(enabled: bool) -> Settings:
    settings = load_settings()
    settings.automation_enabled = enabled
    save_settings(settings)
    return settings


def current_slot_key(now: datetime | None = None) -> str | None:
    """現在が送信スロットなら '09:00' / '00:00' / '19:00' など."""
    now = now or datetime.now(JST)
    for slot_key, (hour, minute_limit) in SLOT_WINDOWS.items():
        if now.hour == hour and now.minute < minute_limit:
            return slot_key
    return None


def weekday_key(now: datetime | None = None) -> str:
    now = now or datetime.now(JST)
    return WEEKDAYS[now.weekday()]


def is_scheduled_now(settings: Settings, now: datetime | None = None) -> bool:
    if not settings.automation_enabled:
        return False
    now = now or datetime.now(JST)
    slot = current_slot_key(now)
    if not slot:
        return False
    day = weekday_key(now)
    return bool(settings.schedule.get(day, {}).get(slot))


def run_slot_id(now: datetime | None = None) -> str:
    now = now or datetime.now(JST)
    slot = current_slot_key(now) or "manual"
    return f"{now.date().isoformat()}_{slot}"
