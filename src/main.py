#!/usr/bin/env python3
"""ミテネ！自動送信のエントリポイント."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from mitene_sender import DailyLimitReached
from runner import build_sender

ROOT = Path(__file__).resolve().parent.parent


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="姫デコ ミテネ！自動送信")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="送信せず画面遷移のみ確認")
    parser.add_argument("--headed", action="store_true", help="ブラウザを表示")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    setup_logging(args.verbose)

    login_id = os.getenv("HIMEDECO_LOGIN_ID", "").strip()
    password = os.getenv("HIMEDECO_PASSWORD", "").strip()
    base_url = os.getenv("HIMEDECO_BASE_URL", "").strip()
    auth_path = os.getenv("AUTH_STATE_PATH", "playwright/.auth/himedeco.json")

    if not login_id or not password:
        logging.error(".env に HIMEDECO_LOGIN_ID / HIMEDECO_PASSWORD を設定してください")
        return 1
    if not base_url:
        logging.error(
            ".env に HIMEDECO_BASE_URL を設定してください（姫デコのログインページURL）"
        )
        return 1

    sender = build_sender(
        base_url=base_url,
        login_id=login_id,
        password=password,
        account_id="cli",
        dry_run=args.dry_run,
        headed=args.headed,
    )

    try:
        sender.run()
    except DailyLimitReached as e:
        logging.warning("%s", e)
        return 0
    except Exception as e:
        logging.exception("送信処理でエラー: %s", e)
        return 1

    logging.info("処理が正常終了しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
