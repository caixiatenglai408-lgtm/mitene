"""アプリのパス解決（開発環境 / PyInstaller 凍結 exe）."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """設定・data・logs を置くルート（exe と同じフォルダ）."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """同梱リソース（templates / static / 初期 config）."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return app_root()


APP_ROOT = app_root()
BUNDLE_ROOT = bundle_root()


def playwright_browsers_dir() -> Path:
    return APP_ROOT / "playwright-browsers"


def setup_runtime() -> None:
    """起動時の環境変数・フォルダを整える."""
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        str(playwright_browsers_dir()),
    )
    seed_user_files()


def seed_user_files() -> None:
    """初回起動時に config 等を exe 横へコピー."""
    for name in ("config.yaml", ".env.example"):
        src = BUNDLE_ROOT / name
        dest = APP_ROOT / name
        if src.exists() and not dest.exists():
            shutil.copy2(src, dest)

    for rel in ("data", "logs", "playwright/.auth"):
        (APP_ROOT / rel).mkdir(parents=True, exist_ok=True)

    playwright_browsers_dir().mkdir(parents=True, exist_ok=True)
