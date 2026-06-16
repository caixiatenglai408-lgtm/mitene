#!/usr/bin/env python3
"""DECOアイコンを高解像度PNGで書き出す（角は透過）."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "web/static/icons/deco-app-icon.svg"
OUT_DIR = ROOT / "web/static/icons"


def export_rsvg(sizes: list[int]) -> bool:
    for size in sizes:
        out = OUT_DIR / f"deco-app-icon-{size}.png"
        cmd = ["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(out), str(SVG)]
        try:
            subprocess.run(cmd, check=True)
            print(f"OK: {out}")
        except FileNotFoundError:
            return False
        except subprocess.CalledProcessError as e:
            print(f"失敗: {e}", file=sys.stderr)
            return False
    return True


def export_cairosvg(sizes: list[int]) -> bool:
    try:
        import cairosvg
    except ImportError:
        return False
    for size in sizes:
        out = OUT_DIR / f"deco-app-icon-{size}.png"
        cairosvg.svg2png(url=str(SVG), write_to=str(out), output_width=size, output_height=size)
        print(f"OK: {out}")
    return True


def main() -> int:
    if not SVG.exists():
        print(f"見つかりません: {SVG}", file=sys.stderr)
        return 1
    sizes = [512, 1024]
    if export_rsvg(sizes):
        return 0
    if export_cairosvg(sizes):
        return 0
    print(
        "PNG書き出しには rsvg-convert または cairosvg が必要です:\n"
        "  brew install librsvg\n"
        "  または pip install cairosvg",
        file=sys.stderr,
    )
    print(f"SVG（透過・高画質）: {SVG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
