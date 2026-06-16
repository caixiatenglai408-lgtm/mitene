#!/usr/bin/env python3
"""DECOアイコンをアプリ用アセット（icns / favicon）に変換."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "web/static/icons/deco-app-icon-1024.png"
STATIC = ROOT / "web/static"
ICNS_OUT = STATIC / "icon.icns"
FAVICON_OUT = STATIC / "favicon.ico"
FAVICON_PNG = STATIC / "favicon.png"


def build_icns(src: Image.Image, out: Path) -> None:
    src.save(out, format="ICNS")
    print(f"OK: {out}")


def build_favicon(src: Image.Image, out: Path) -> None:
    sizes = [(16, 16), (32, 32), (48, 48)]
    src.save(out, format="ICO", sizes=sizes)
    print(f"OK: {out}")


def build_favicon_png(src: Image.Image, out: Path) -> None:
    img = src.resize((32, 32), Image.Resampling.LANCZOS)
    img.save(out, "PNG")
    print(f"OK: {out}")


def main() -> int:
    if not SRC.exists():
        print(f"見つかりません: {SRC}", file=sys.stderr)
        print("先に scripts/process_deco_icon.py を実行してください。", file=sys.stderr)
        return 1
    src = Image.open(SRC).convert("RGBA")
    build_icns(src, ICNS_OUT)
    build_favicon(src, FAVICON_OUT)
    build_favicon_png(src, FAVICON_PNG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
