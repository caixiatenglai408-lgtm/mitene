#!/usr/bin/env python3
"""Playwright の Chromium が無ければインストールする."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from playwright_setup import ensure_chromium  # noqa: E402


def main() -> int:
    return ensure_chromium()


if __name__ == "__main__":
    raise SystemExit(main())
