"""Vercel entrypoint for the Flask dashboard."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SRC = ROOT / "src"

for path in (ROOT, _SRC):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from web.app import app  # noqa: E402
