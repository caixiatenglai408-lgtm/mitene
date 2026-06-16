"""Playwright Chromium の有無確認とインストール."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app_paths import is_frozen, playwright_browsers_dir, setup_runtime


def chromium_ready() -> bool:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        exe = p.chromium.executable_path
    if not os.path.isfile(exe):
        return False
    if sys.platform == "win32":
        return True
    return os.access(exe, os.X_OK)


def _install_chromium_command() -> list[str]:
    if not is_frozen():
        return [sys.executable, "-m", "playwright", "install", "chromium"]

    import playwright

    driver_dir = Path(playwright.__file__).resolve().parent / "driver"
    node = driver_dir / ("node.exe" if sys.platform == "win32" else "node")
    cli = driver_dir / "package" / "cli.js"
    return [str(node), str(cli), "install", "chromium"]


def ensure_chromium() -> int:
    setup_runtime()
    try:
        ready = chromium_ready()
    except ImportError:
        print("Playwright が見つかりません。")
        return 1

    if ready:
        print("ブラウザ（Chromium）はすでにインストール済みです。")
        print(f"保存先: {playwright_browsers_dir()}")
        return 0

    print("")
    print("==========================================")
    print("  Chromium をダウンロードします")
    print("  （数分かかることがあります）")
    print(f"  保存先: {playwright_browsers_dir()}")
    print("==========================================")
    print("")
    env = os.environ.copy()
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(playwright_browsers_dir()))
    return subprocess.call(_install_chromium_command(), env=env)
