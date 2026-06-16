#!/usr/bin/env python3
"""添付DECOアイコンを高解像度化し、丸角外のグレーを透過する."""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "web/static/icons"
DEFAULT_SRC = ROOT / "web/static/icons/deco-source.png"
SIZES = (512, 1024)
# iOS風スクイクル（1024基準で rx≈230）
CORNER_RATIO = 230 / 1024


def rounded_rect_mask(size: int, radius: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    r = int(round(radius))
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=r, fill=255)
    return mask


def remove_gray_background(img: Image.Image) -> Image.Image:
    """角付近のグレー背景を透過（赤タイルは残す）."""
    rgba = img.convert("RGBA")
    px = rgba.load()
    w, h = rgba.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            # 中性グレー（背景）: R≈G≈B で赤成分が少ない
            if abs(int(r) - int(g)) < 18 and abs(int(g) - int(b)) < 18:
                avg = (int(r) + int(g) + int(b)) // 3
                if 95 < avg < 210 and max(r, g, b) - min(r, g, b) < 25:
                    px[x, y] = (r, g, b, 0)
    return rgba


def upscale(img: Image.Image, size: int) -> Image.Image:
    up = img.resize((size, size), Image.Resampling.LANCZOS)
    # 軽いシャープでぼやけを抑える
    return up.filter(ImageFilter.UnsharpMask(radius=1.2, percent=130, threshold=2))


def process(src: Path, out_dir: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(src)
    base = Image.open(src).convert("RGBA")
    base = remove_gray_background(base)

    for size in SIZES:
        img = upscale(base, size)
        radius = size * CORNER_RATIO
        mask = rounded_rect_mask(size, radius)
        img.putalpha(mask)
        out = out_dir / f"deco-app-icon-{size}.png"
        img.save(out, "PNG", optimize=True)
        print(f"OK: {out} ({size}x{size}, 透過)")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    out_dir = OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        process(src, out_dir)
    except FileNotFoundError:
        print(f"元画像が見つかりません: {src}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
