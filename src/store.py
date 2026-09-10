"""設定・女の子アカウントの永続化."""

from __future__ import annotations

import locale as _locale
import re as _re
import unicodedata as _ud
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app_paths import APP_ROOT, DATA_ROOT, auth_root
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
JST = ZoneInfo("Asia/Tokyo")


@dataclass
class Account:
    id: str
    name: str
    login_id: str
    password: str
    # 並び順用の読み仮名（ひらがな・任意）。漢字→かなの自動推測はしない。
    # 未設定（""）は後方互換。cast_sort_key で「未設定グループ」として末尾に寄せる。
    name_kana: str = ""
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "name_kana": self.name_kana,
            "login_id": self.login_id,
            "enabled": self.enabled,
            "has_password": bool(self.password),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Settings:
    base_url: str = ""
    # 直近の送信結果（手動送信の表示用）
    last_run: dict[str, Any] | None = None
    # 本日出勤（チェックした女の子を優先送信）— attendance_date とセットで保存
    attendance_date: str = ""
    working_today_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "base_url": self.base_url,
            "attendance_date": self.attendance_date,
            "working_today_ids": list(self.working_today_ids),
        }
        if self.last_run:
            d["last_run"] = self.last_run
        return d


def _now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    _ensure_data_dir()
    raw = _read_settings_payload(SETTINGS_PATH)
    if raw is None:
        return Settings()
    last_run = raw.get("last_run")
    # 旧キー（automation_enabled / schedule / last_run_slot / sleep_schedule_enabled）は
    # 読み込まない。次回 save_settings 時に to_dict() から自然に落ちる（migration 不要）。
    return Settings(
        base_url=str(raw.get("base_url", "")),
        last_run=last_run if isinstance(last_run, dict) else None,
        attendance_date=str(raw.get("attendance_date", "")),
        working_today_ids=[
            str(i) for i in (raw.get("working_today_ids") or []) if str(i)
        ],
    )


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
    _ensure_data_dir()
    _write_settings_payload(
        SETTINGS_PATH,
        settings.to_dict(),
    )


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
                name_kana=str(row.get("name_kana", "") or ""),
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


# \u65e5\u672c\u8a9e\u30ed\u30b1\u30fc\u30eb\uff08\u7167\u5408\u7528\u9014\u306e\u307f\u30fb\u5931\u6557\u3057\u3066\u3082\u81f4\u547d\u7684\u3067\u306a\u3044\uff09
for _loc in ("ja_JP.UTF-8", "ja_JP.utf8", "ja_JP", "ja"):
    try:
        _locale.setlocale(_locale.LC_COLLATE, _loc)
        break
    except _locale.Error:
        continue

# \u5168\u89d2\u30ab\u30bf\u30ab\u30ca \u2192 \u3072\u3089\u304c\u306a\uff08\u304b\u306a\u540d\u3092\u300c\u3042\u3044\u3046\u3048\u304a\u300d\u9806\u306b\u305d\u308d\u3048\u308b\u3002\u8aad\u307f\u4eee\u540dDB\u306f\u4f5c\u3089\u306a\u3044\uff09
# \u203b str.translate \u306f\u30b3\u30fc\u30c9\u30dd\u30a4\u30f3\u30c8\uff08int\uff09\u30ad\u30fc\u306e\u5bfe\u5fdc\u8868\u304c\u5fc5\u8981
_KANA_FOLD = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}  # \u30a1..\u30f6 \u2192 \u3041..\u3096
_KANA_FOLD[0x30FD] = 0x309D  # \u30fd \u2192 \u309d
_KANA_FOLD[0x30FE] = 0x309E  # \u30fe \u2192 \u309e


def account_display_sort_key(name: str):
    """\u767b\u9332\u4e00\u89a7\u306e\u8868\u793a\u9806\uff08\u3042\u3044\u3046\u3048\u304a\u9806\u76f8\u5f53\u30fb\u5b89\u5b9a\u30bd\u30fc\u30c8\uff09.

    \u8aad\u307f\u4eee\u540dDB\uff0f\u8aad\u307f\u4eee\u540d\u5165\u529b\u6b04\u306f\u4f5c\u3089\u306a\u3044\u3002\u5168\u89d2\u30ab\u30ca\u3092\u3072\u3089\u304c\u306a\u3078\u7573\u3093\u3060\u4e0a\u3067
    \u6b63\u898f\u5316\u6587\u5b57\u5217\u9806\u306b\u4e26\u3079\u308b\uff08\u304b\u306a\u540d\u306f\u300c\u3042\u3044\u3046\u3048\u304a\u300d\u9806\u3001\u6f22\u5b57\u306f\u8868\u8a18\u306e\u307e\u307e
    \u30b3\u30fc\u30c9\u30dd\u30a4\u30f3\u30c8\u9806\uff09\u3002\u7a7a\u306e\u8868\u793a\u540d\u306f\u672b\u5c3e\u3002\u4fdd\u5b58\u9806\u306f\u4e00\u5207\u5909\u66f4\u3057\u306a\u3044\u3002
    """
    norm = normalize_display_name(name)
    folded = norm.translate(_KANA_FOLD).casefold()
    return (norm == "", folded)


def normalize_sort_kana(text: str) -> str:
    """並び順用の読み仮名を正規化する（漢字→かなの推測は一切しない）.

    - NFKC 正規化（全角/半角・互換文字を統一）
    - カタカナ → ひらがな（_KANA_FOLD）
    - 空白（半角/全角/タブ等）を完全除去
      → 「あきよし れん」「あきよし　れん」「あきよしれん」を同一の並び位置にする
    - casefold（英字混在時の安定化）
    未設定（空文字）はそのまま "" を返す。
    """
    if not text:
        return ""
    s = _ud.normalize("NFKC", str(text))
    s = s.translate(_KANA_FOLD)
    s = _re.sub(r"\s+", "", s)
    return s.casefold()


def cast_sort_key(account: "Account") -> tuple:
    """キャスト並び順の source of truth（登録一覧・出勤チェック・全員送信 共通）.

    第一: 読み仮名（normalize_sort_kana 済み）。未設定は「未設定グループ」として末尾へ。
    第二: 表示名のフォールバックキー（account_display_sort_key）。
    第三: 安定 id（同じ読み仮名でも順序が毎回変わらない deterministic sort）。

    漢字→かなの自動推測はしない。読み仮名未設定を「正式な五十音順」とはみなさず、
    kana 設定済みキャストより後ろへまとめる（アプリは壊さない fallback）。
    """
    kana = normalize_sort_kana(getattr(account, "name_kana", "") or "")
    name_key = account_display_sort_key(getattr(account, "name", "") or "")
    has_no_kana = 1 if kana == "" else 0
    return (has_no_kana, kana, name_key, str(getattr(account, "id", "")))


def accounts_sorted_for_display(
    accounts: list["Account"] | None = None,
) -> list["Account"]:
    src = accounts if accounts is not None else load_accounts()
    return sorted(src, key=cast_sort_key)


def upsert_account(
    name: str,
    login_id: str,
    password: str,
    account_id: str | None = None,
    enabled: bool = True,
    name_kana: str | None = None,
) -> Account:
    accounts = load_accounts()
    now = _now_iso()
    kana_in = ("" if name_kana is None else str(name_kana)).strip()
    if account_id:
        for i, a in enumerate(accounts):
            if a.id == account_id:
                accounts[i] = Account(
                    id=a.id,
                    name=name.strip(),
                    login_id=login_id.strip(),
                    password=password if password else a.password,
                    # name_kana 未指定（None）なら既存値を保持。空文字は「クリア」を許可。
                    name_kana=a.name_kana if name_kana is None else kana_in,
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
        name_kana=kana_in,
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
                name_kana=a.name_kana,
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
    auth_file = auth_root() / f"{account_id}.json"
    if auth_file.exists():
        auth_file.unlink()
    return True


def attendance_today_key(now: datetime | None = None) -> str:
    now = now or datetime.now(JST)
    return now.strftime("%Y-%m-%d")


def attendance_today_label(now: datetime | None = None) -> str:
    now = now or datetime.now(JST)
    wd = WEEKDAY_LABELS[WEEKDAYS[now.weekday()]]
    return f"{now.month}月{now.day}日（{wd}）"


def _sync_attendance_date(settings: Settings) -> bool:
    """日付が変わっていたら出勤チェックをリセット。変更があれば True."""
    today = attendance_today_key()
    if settings.attendance_date == today:
        return False
    settings.attendance_date = today
    settings.working_today_ids = []
    return True


def _prune_working_today_ids(settings: Settings) -> bool:
    valid = {a.id for a in load_accounts()}
    pruned = [i for i in settings.working_today_ids if i in valid]
    if pruned == settings.working_today_ids:
        return False
    settings.working_today_ids = pruned
    return True


def load_working_today_ids() -> set[str]:
    settings = load_settings()
    changed = _sync_attendance_date(settings)
    changed = _prune_working_today_ids(settings) or changed
    if changed:
        save_settings(settings)
    return set(settings.working_today_ids)


def set_account_working_today(account_id: str, working: bool) -> Settings:
    if get_account(account_id) is None:
        raise ValueError("アカウントが見つかりません")
    settings = load_settings()
    _sync_attendance_date(settings)
    ids = list(settings.working_today_ids)
    if working:
        if account_id not in ids:
            ids.append(account_id)
    else:
        ids = [i for i in ids if i != account_id]
    settings.working_today_ids = ids
    save_settings(settings)
    return settings


def reset_working_today() -> Settings:
    """登録一覧の出勤チェックをすべて外す."""
    settings = load_settings()
    settings.attendance_date = attendance_today_key()
    settings.working_today_ids = []
    save_settings(settings)
    return settings


def order_accounts_by_attendance(accounts: list[Account]) -> list[Account]:
    """本日出勤 → お休み の順。各グループ内は cast_sort_key（五十音順）で統一."""
    working_ids = load_working_today_ids()
    working = sorted(
        (a for a in accounts if a.id in working_ids), key=cast_sort_key
    )
    off = sorted(
        (a for a in accounts if a.id not in working_ids), key=cast_sort_key
    )
    return working + off




def partition_enabled_by_attendance() -> tuple[list[Account], list[Account]]:
    """送信対象（enabled）を「本日出勤 / お休み」へ分割.

    各グループ内は cast_sort_key（読み仮名ベースの五十音順・deterministic）で整列する。
    これが「登録一覧の表示」「出勤チェックパネル」「今すぐ全員送信の処理順」すべての
    共通の並び順になる（UI 表示順 == 実送信順）。
    """
    enabled = [a for a in load_accounts() if a.enabled]
    working_ids = load_working_today_ids()
    working = sorted(
        (a for a in enabled if a.id in working_ids), key=cast_sort_key
    )
    off = sorted(
        (a for a in enabled if a.id not in working_ids), key=cast_sort_key
    )
    return working, off


def accounts_for_manual_group(group: str) -> list[Account]:
    """手動送信の処理順（本日出勤→お休み。各グループ内は五十音順）.

    partition_enabled_by_attendance が cast_sort_key で整列済みなので、
    ここでは順序を作り直さない（UI と同一順）。
    """
    working, off = partition_enabled_by_attendance()
    if group == "working":
        return list(working)
    if group == "off":
        return list(off)
    return list(working) + list(off)
