"""Vercel entrypoint.

Vercel's Python runtime serves WSGI apps exported as `app`.
We reuse the existing Flask dashboard defined in `web/app.py`.
"""

from web.app import app  # noqa: F401

