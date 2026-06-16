#!/usr/bin/env python3
"""ローカルアプリとして起動（専用ウィンドウ or ブラウザ）."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))

from app_paths import APP_ROOT, BUNDLE_ROOT, is_frozen, setup_runtime  # noqa: E402

ROOT = APP_ROOT


def wait_for_port(port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def ensure_venv() -> Path:
    if is_frozen():
        return Path(sys.executable)
    venv_python = ROOT / ".venv" / "bin" / "python"
    if sys.platform == "win32":
        venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return venv_python
    print("初回セットアップ中（数分かかります）...")
    subprocess.check_call([sys.executable, "-m", "venv", str(ROOT / ".venv")])
    subprocess.check_call(
        [str(venv_python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]
    )
    subprocess.check_call([str(venv_python), "-m", "playwright", "install", "chromium"])
    return venv_python


def stop_existing(port: int) -> None:
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                check=False,
            )
            pids: set[str] = set()
            needle = f":{port}"
            for line in result.stdout.splitlines():
                if needle not in line or "LISTENING" not in line.upper():
                    continue
                parts = line.split()
                if parts:
                    pids.add(parts[-1])
            for pid in pids:
                if pid.isdigit() and pid != "0":
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True,
                        check=False,
                    )
            if pids:
                time.sleep(0.5)
        except FileNotFoundError:
            pass
        return

    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            check=False,
        )
        pids = [p for p in result.stdout.strip().split() if p]
        for pid in pids:
            subprocess.run(["kill", pid], check=False)
        if pids:
            time.sleep(1)
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            check=False,
        )
        for pid in result.stdout.strip().split():
            if pid:
                subprocess.run(["kill", "-9", pid], check=False)
        if result.stdout.strip():
            time.sleep(0.5)
    except FileNotFoundError:
        pass


def resolve_browser_app(*, chrome: bool, browser_app: str | None) -> str | None:
    if chrome:
        return "Google Chrome"
    if browser_app:
        return browser_app.strip() or None
    env = os.getenv("MITENE_BROWSER_APP", "").strip()
    return env or None


def open_in_browser(url: str, browser_app: str | None = None) -> None:
    if sys.platform == "darwin":
        if browser_app:
            result = subprocess.run(
                ["open", "-a", browser_app, url],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                err = (result.stderr or "").strip()
                print(
                    f"警告: 「{browser_app}」で開けませんでした。"
                    + (f" ({err})" if err else "")
                    + " 標準ブラウザで開きます…"
                )
                subprocess.run(["open", url], check=False)
            else:
                print(f"ブラウザ: {browser_app}")
        else:
            subprocess.run(["open", url], check=False)
    else:
        webbrowser.open(url)


def start_server(python: Path, port: int, *, auto_exit: bool = False) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT / 'src'}:{env.get('PYTHONPATH', '')}"
    env["PORT"] = str(port)
    if auto_exit:
        env["MITENE_AUTO_EXIT"] = "1"

    if is_frozen():
        cmd = [str(python), "--server-only", f"--port={port}"]
        if auto_exit:
            cmd.append("--auto-exit")
        return subprocess.Popen(cmd, cwd=str(ROOT), env=env)

    return subprocess.Popen(
        [str(python), str(ROOT / "web" / "app.py")],
        cwd=str(ROOT),
        env=env,
    )


def wait_until_browser_tabs_closed(
    port: int, proc: subprocess.Popen, *, idle_sec: float = 45.0
) -> None:
    status_url = f"http://127.0.0.1:{port}/api/client-heartbeat/status"
    had_client = False
    idle_since: float | None = None
    idle_streak = 0
    waiting_for_work = False
    last_busy_at: float | None = None

    while proc.poll() is None:
        active_count = None
        busy = None
        try:
            with urllib.request.urlopen(status_url, timeout=3) as resp:
                payload = json.loads(resp.read().decode())
            active_count = int(payload.get("active_count", 0))
            busy = bool(payload.get("busy"))
        except (urllib.error.URLError, ValueError, OSError):
            idle_since = None
            idle_streak = 0
            time.sleep(2.0)
            continue

        if busy:
            last_busy_at = time.time()
            idle_since = None
            idle_streak = 0
            if had_client and active_count == 0 and not waiting_for_work:
                waiting_for_work = True
                print("\n送信中のため、完了までサーバーを動かします…")
            time.sleep(1.5)
            continue

        waiting_for_work = False

        if active_count > 0:
            had_client = True
            idle_since = None
            idle_streak = 0
        elif had_client:
            if last_busy_at and time.time() - last_busy_at < 120.0:
                idle_since = None
                idle_streak = 0
            else:
                idle_streak += 1
                if idle_since is None:
                    idle_since = time.time()
                elif idle_streak >= 5 and time.time() - idle_since >= idle_sec:
                    print("\n管理画面のタブを閉じたため、終了します…")
                    break

        time.sleep(1.5)


def run_webview(url: str) -> None:
    import webview

    webview.create_window(
        "ミテネ自動送信",
        url,
        width=440,
        height=860,
        min_size=(360, 640),
        resizable=True,
    )
    webview.start()


def _load_dashboard_module():
    if is_frozen():
        app_py = BUNDLE_ROOT / "web" / "app.py"
    else:
        app_py = ROOT / "web" / "app.py"
    spec = importlib.util.spec_from_file_location("mitene_dashboard", app_py)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"管理画面を読み込めません: {app_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_server_only(port: int) -> int:
    setup_runtime()
    os.chdir(ROOT)
    os.environ["PORT"] = str(port)
    module = _load_dashboard_module()
    module.main()
    return 0


def run_scheduled() -> int:
    setup_runtime()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / "src"))
    import logging

    from scheduler_service import run_all_scheduled

    log_path = ROOT / "logs" / "scheduled.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    results = run_all_scheduled()
    if not results:
        logging.info("送信対象なし（OFF・時間外・実行済みなど）")
        return 0
    if results and results[0].get("error"):
        logging.warning("スキップ: %s", results[0]["error"])
        return 0
    logging.info("完了: %d 件", len(results))
    return 0


def run_install_browsers() -> int:
    os.chdir(ROOT)
    from playwright_setup import ensure_chromium

    return ensure_chromium()


def main() -> int:
    parser = argparse.ArgumentParser(description="ミテネ自動送信（ローカルアプリ）")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="ブラウザのタブで開く（従来の起動.command と同じ）",
    )
    parser.add_argument(
        "--chrome",
        action="store_true",
        help="Google Chrome で開く（macOS）",
    )
    parser.add_argument(
        "--browser-app",
        metavar="NAME",
        help='使用するブラウザ名（例: "Google Chrome", "Safari"）',
    )
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "5050")))
    args = parser.parse_args()
    browser_app = resolve_browser_app(chrome=args.chrome, browser_app=args.browser_app)

    setup_runtime()
    port = args.port
    url = f"http://127.0.0.1:{port}"
    os.chdir(ROOT)
    python = ensure_venv()

    proc = None
    for attempt in range(2):
        stop_existing(port)
        proc = start_server(python, port, auto_exit=args.browser)
        if wait_for_port(port, timeout=15.0):
            break
        if proc.poll() is not None:
            proc.wait(timeout=3)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if attempt == 0:
            print("ポートが使用中のため、既存プロセスを終了して再試行します…")
    else:
        print(f"エラー: サーバーが起動しませんでした ({url})")
        if sys.platform == "win32":
            print("ヒント: タスクマネージャーで MiteneAutoSend.exe を終了してください")
        else:
            print("ヒント: 起動.command を終了するか、ターミナルで以下を実行:")
            print(f"  lsof -ti :{port} | xargs kill")
        return 1

    try:
        print(f"起動しました: {url}")

        if args.browser:
            open_in_browser(url, browser_app=browser_app)
            print("終了: 管理画面のタブをすべて閉じる（または Ctrl+C）")
            wait_until_browser_tabs_closed(port, proc)
        else:
            try:
                run_webview(url)
            except ImportError:
                print("pywebview がありません。ブラウザで開きます…")
                open_in_browser(url, browser_app=browser_app)
                proc.wait()
    except KeyboardInterrupt:
        print("\n終了します…")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return 0


def _bootstrap_from_argv() -> int | None:
    if len(sys.argv) < 2:
        return None
    mode = sys.argv[1]
    if mode == "--server-only":
        port = int(os.getenv("PORT", "5050"))
        for arg in sys.argv[2:]:
            if arg.startswith("--port="):
                port = int(arg.split("=", 1)[1])
        return run_server_only(port)
    if mode == "--scheduled":
        return run_scheduled()
    if mode == "--install-browsers":
        return run_install_browsers()
    return None


if __name__ == "__main__":
    code = _bootstrap_from_argv()
    if code is None:
        code = main()
    sys.exit(code)
