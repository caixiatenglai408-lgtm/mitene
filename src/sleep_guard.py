"""送信中・ジョブ実行中は OS のスリープを抑止する."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_depth = 0
_caffeinate_proc: subprocess.Popen[Any] | None = None
_windows_state_active = False


def _windows_start() -> None:
    global _windows_state_active
    if sys.platform != "win32":
        return
    import ctypes

    # システムを起きたままにする（画面オフでも処理は継続）
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    )
    _windows_state_active = True
    logger.info("スリープ抑止を開始しました（Windows）")


def _windows_stop() -> None:
    global _windows_state_active
    if sys.platform != "win32" or not _windows_state_active:
        return
    import ctypes

    ES_CONTINUOUS = 0x80000000
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    _windows_state_active = False
    logger.info("スリープ抑止を解除しました（Windows）")


def _darwin_start() -> None:
    global _caffeinate_proc
    if sys.platform != "darwin":
        return
    if _caffeinate_proc is not None and _caffeinate_proc.poll() is None:
        return
    _caffeinate_proc = subprocess.Popen(
        ["caffeinate", "-dims"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("スリープ抑止を開始しました（macOS caffeinate）")


def _darwin_stop() -> None:
    global _caffeinate_proc
    if sys.platform != "darwin" or _caffeinate_proc is None:
        return
    if _caffeinate_proc.poll() is None:
        _caffeinate_proc.terminate()
        try:
            _caffeinate_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _caffeinate_proc.kill()
    _caffeinate_proc = None
    logger.info("スリープ抑止を解除しました（macOS）")


def _platform_start() -> None:
    if sys.platform == "win32":
        _windows_start()
    elif sys.platform == "darwin":
        _darwin_start()


def _platform_stop() -> None:
    if sys.platform == "win32":
        _windows_stop()
    elif sys.platform == "darwin":
        _darwin_stop()


def acquire() -> None:
    """送信中など、スリープさせたくない処理の開始."""
    global _depth
    with _lock:
        _depth += 1
        if _depth == 1:
            _platform_start()


def release() -> None:
    """スリープ抑止の終了."""
    global _depth
    with _lock:
        if _depth <= 0:
            return
        _depth -= 1
        if _depth == 0:
            _platform_stop()


class guard:
    """with sleep_guard.guard(): で使う."""

    def __enter__(self) -> guard:
        acquire()
        return self

    def __exit__(self, *args: object) -> None:
        release()
