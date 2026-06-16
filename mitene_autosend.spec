# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller ビルド定義（Windows .exe 向け）.

使い方（Windows PC 上）:
  pip install -r requirements.txt -r requirements-build.txt
  pyinstaller mitene_autosend.spec
"""

from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "web" / "templates"), "web/templates"),
    (str(ROOT / "web" / "static"), "web/static"),
    (str(ROOT / "config.yaml"), "."),
    (str(ROOT / ".env.example"), "."),
]

binaries: list = []
hiddenimports = [
    "app_paths",
    "playwright_setup",
    "store",
    "runner",
    "job_runner",
    "scheduler_service",
    "mitene_sender",
    "human_behavior",
    "crypto_util",
    "result_display",
    "platform_schedule",
    "platform_schedule.common",
    "platform_schedule.windows",
    "platform_schedule.darwin",
    "flask",
    "werkzeug",
    "jinja2",
    "yaml",
    "dotenv",
    "cryptography",
    "apscheduler",
    "zoneinfo",
    "webview",
]
hiddenimports += collect_submodules("playwright")

for package in ("playwright", "flask", "cryptography", "tzdata", "webview"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hidden
    except Exception:
        pass

icon_path = ROOT / "web" / "static" / "favicon.ico"
icon_arg = str(icon_path) if icon_path.exists() else None

a = Analysis(
    [str(ROOT / "launch_app.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MiteneAutoSend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MiteneAutoSend",
)
