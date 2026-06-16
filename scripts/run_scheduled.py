#!/usr/bin/env python3
"""定時送信（launchd / 手動）。管理画面が起動していなくても実行可能."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from scheduler_service import run_all_scheduled  # noqa: E402

LOG_PATH = ROOT / "logs" / "scheduled.log"


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
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


if __name__ == "__main__":
    sys.exit(main())
