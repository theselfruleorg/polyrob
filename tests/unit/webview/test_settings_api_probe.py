"""030 D-3 pin — the deployed webview serves /settings without the :9000 API.

settings.js fetches /api/mcp/settings and /api/skills/*, which only the API
service mounts (api/app.py) — the webview process never builds the container
services they need (user_mcp_service / mcp), so mounting those routers here
would 503, not work. The chosen honest-cheap fix is CLIENT-side: settings.js
probes the API surface once and renders an explicit "needs the API service"
state instead of console errors + fake-empty lists. This test pins that
wiring so a refactor can't silently regress the page back to 404 spam.
"""
from pathlib import Path

_WEBVIEW = Path(__file__).resolve().parents[3] / "webview"


def test_settings_js_probes_api_and_renders_honest_state():
    js = (_WEBVIEW / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    assert "probeApiService" in js
    assert "renderApiUnavailable" in js
    # the probe result actually gates rendering (called from init)
    assert "apiServiceAvailable = await probeApiService()" in js
    # the honest state names the real dependency, not a generic error
    assert "API service" in js


def test_settings_template_loads_settings_js():
    html = (_WEBVIEW / "templates" / "settings.html").read_text(encoding="utf-8")
    assert "/static/js/settings.js" in html


def test_webview_server_does_not_mount_api_service_routers():
    """The decision itself: the webview process must NOT import/mount the MCP/
    skills routers (their container deps don't exist here — they would 503).
    If someone later mounts them properly, update the D-3 story AND this pin."""
    src = (_WEBVIEW / "server.py").read_text(encoding="utf-8")
    assert "mcp_routes" not in src
    assert "skill_endpoints" not in src
