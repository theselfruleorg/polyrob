"""Owner-console deep links (QW-3, 2026-07-19 / proposal 021).

``WEBVIEW_PUBLIC_URL`` names the owner-auth webview base (e.g.
``https://console.example.com``). Unset => no links are ever emitted (a plain
server without a public console stays byte-identical). Links carry NO
credential material — the console's own owner login gates access.
"""
import os
from typing import Optional


def webview_public_url() -> Optional[str]:
    base = (os.getenv("WEBVIEW_PUBLIC_URL") or "").strip().rstrip("/")
    return base or None


def webview_session_link(session_id: str) -> Optional[str]:
    """Stable session deep link (``/session/<id>``, webview/server.py) or None."""
    base = webview_public_url()
    if not base or not session_id:
        return None
    return f"{base}/session/{session_id}"
