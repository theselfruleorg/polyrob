"""The webview process must NOT mount the API-service MCP/skills routers.

043 §9 phase 4 deleted the legacy `/settings` page and its `settings.js` (the
MCP-servers + skills panel moved to the new Agent destination). The 030 D-3 page
pins (settings.js probes the API and renders an honest "needs the API service"
state; settings.html loads settings.js) went with the deleted surface.

What survives is the server-boundary DECISION those pins protected: the webview
process still does not import/mount the MCP/skills routers, because their
container deps don't exist here and mounting them would 503. That invariant is
independent of any one page, so it stays pinned.
"""
from pathlib import Path

_WEBVIEW = Path(__file__).resolve().parents[3] / "webview"


def test_webview_server_does_not_mount_api_service_routers():
    """The decision itself: the webview process must NOT import/mount the MCP/
    skills routers (their container deps don't exist here — they would 503).
    If someone later mounts them properly, update the D-3 story AND this pin."""
    src = (_WEBVIEW / "server.py").read_text(encoding="utf-8")
    assert "mcp_routes" not in src
    assert "skill_endpoints" not in src
