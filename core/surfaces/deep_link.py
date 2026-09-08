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


def webview_artifact_link(session_id: str, rel_path: str) -> Optional[str]:
    """Deep link that SERVES one workspace file (``webview/server.py::
    api_workspace_serve``), or None when no console is configured.

    A filesystem path is not an address: the owner cannot open
    ``/var/lib/polyrob/project/report.md`` from a phone. This route already
    serves workspace files behind the console's owner auth, so the honest
    answer for a file that could not be attached is this link, not the path
    (chat-first review 2026-08-22, G3).
    """
    from urllib.parse import quote
    base = webview_public_url()
    if not base or not session_id or not rel_path:
        return None
    safe = quote(str(rel_path).lstrip("/"), safe="/")
    return f"{base}/api/session/{session_id}/workspace/serve/{safe}"
