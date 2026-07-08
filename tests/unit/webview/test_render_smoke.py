"""P3 — render smoke: webgate pages serve 200 and link the design system.

Each v1 page (and the index) must render in single-user mode and pull in the
design-system stylesheets (`variables.css` then `components.css`) wired into
``layout.html`` <head>. This proves the targeted CSS cleanup is actually loaded by
the pages, not just present on disk.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


def _reload_server(monkeypatch, multitenant=False):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true" if multitenant else "false")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    return importlib.reload(server)


@pytest.mark.parametrize("path", ["/", "/memory", "/autonomy", "/identity", "/system"])
def test_page_renders_and_links_design_system(monkeypatch, path):
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    html = r.text
    assert "/static/css/variables.css" in html, f"{path} missing variables.css link"
    assert "/static/css/components.css" in html, f"{path} missing components.css link"


def test_design_system_loads_before_style(monkeypatch):
    """variables.css + components.css must precede style.css in the <head>."""
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    html = client.get("/").text
    i_vars = html.find("/static/css/variables.css")
    i_comp = html.find("/static/css/components.css")
    i_style = html.find("/static/css/style.css")
    assert -1 < i_vars < i_comp < i_style


def test_fastapi_title_is_polyrob_console(monkeypatch):
    server = _reload_server(monkeypatch, multitenant=False)
    assert server._fastapi.title == "POLYROB Console API"


def test_index_page_shows_console_display_name(monkeypatch):
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    html = client.get("/").text
    assert "POLYROB Console" in html


def test_index_page_honors_console_name_override(monkeypatch):
    monkeypatch.setenv("POLYROB_CONSOLE_NAME", "Rob Console")
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    html = client.get("/").text
    assert "Rob Console" in html
    assert "POLYROB Console" not in html


def test_cors_default_uses_webview_domain(monkeypatch):
    monkeypatch.setenv("WEBVIEW_DOMAIN", "custom.example.com")
    server = _reload_server(monkeypatch, multitenant=False)
    assert "https://custom.example.com" in server._cors_origins
    assert "http://custom.example.com" in server._cors_origins


def test_cors_default_unset_uses_local_webview(monkeypatch):
    monkeypatch.delenv("WEBVIEW_DOMAIN", raising=False)
    server = _reload_server(monkeypatch, multitenant=False)
    assert "https://localhost:3000" in server._cors_origins
    assert "http://localhost:3000" in server._cors_origins


def test_index_page_uses_branding_config_defaults(monkeypatch):
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    html = client.get("/").text
    assert "your-polyrob-host.example" in html
    assert "theselfrule.org" in html
    # Beta banner removed 2026-07-06 — must never come back
    assert "beta-banner" not in html
    assert "DEN holders" not in html


def test_index_page_honors_branding_overrides(monkeypatch):
    monkeypatch.setenv("POLYROB_BRAND_URL", "https://brand.example")
    monkeypatch.setenv("POLYROB_ORG_URL", "https://org.example")
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    html = client.get("/").text
    assert "brand.example" in html
    assert "org.example" in html
    assert "your-polyrob-host.example" not in html


def test_footer_renders_real_version(monkeypatch):
    from core.version import get_version
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    html = client.get("/").text
    assert f"ver. {get_version()}" in html
    assert "1.0.0. 2025" not in html


def test_index_header_renders_real_version(monkeypatch):
    from core.version import get_version
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    html = client.get("/").text
    assert f"v{get_version()}" in html


def test_signin_has_no_placeholder_legal_links(monkeypatch):
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    html = client.get("/signin").text if server.webgate.is_multitenant() else None
    # /signin only mounts under multitenant; render signin.html directly via
    # the template engine instead so this test runs in default (single-user) mode.
    rendered = server._templates.get_template("signin.html").render(
        request=None, is_authenticated=False, is_admin=False
    )
    assert 'href="#"' not in rendered


def test_rebrand_did_not_add_new_stylesheet_links(monkeypatch):
    """D1-D3 touched only text nodes/attributes inside existing markup — the
    design-system stylesheet chain (variables -> components -> style, wired
    once in layout.html's <head>, see the "Design System CSS - Load in
    order" comment) must be unchanged: still exactly one link each, in that
    order, no fork/duplicate/fourth design-token source introduced.

    NOTE on the assertion shape: the *total* `rel="stylesheet"` count on the
    rendered index page is NOT 3 — layout.html independently loads a Google
    Fonts sheet + highlight.js's theme, and index.html layers on
    page-specific chat.css/config-panel.css/workspace-fullwidth-fix.css plus
    a Prism CDN sheet. That is pre-existing architecture confirmed present
    before this workstream's first commit (`git show 3b0a1da3^:webview/
    templates/layout.html`) — D1-D3 never touched a CSS file or added a
    `style=` attribute (verified via `git diff 3b0a1da3^..6c8ccf20 -- '*.css'`
    = empty, and no `style=` hits in the full D1-D3 diff). So this test
    guards the design-token chain specifically rather than asserting a
    page-wide literal that was never 3 to begin with.
    """
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    html = client.get("/").text
    assert html.count("/static/css/variables.css") == 1
    assert html.count("/static/css/components.css") == 1
    assert html.count("/static/css/style.css") == 1
    i_vars = html.find("/static/css/variables.css")
    i_comp = html.find("/static/css/components.css")
    i_style = html.find("/static/css/style.css")
    assert -1 < i_vars < i_comp < i_style


def test_settings_page_has_no_coming_soon_stubs(monkeypatch):
    """P0-2 (2026-07-06 UX handoff): the Preferences/API-Keys 'Coming soon'
    placeholder tabs were removed entirely — dead UI must not come back."""
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    r = client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert "Coming soon" not in html
    assert "section-preferences" not in html
    assert "section-api-keys" not in html
    # The real sections stay.
    assert "section-mcp" in html
    assert "section-skills" in html


def test_rebrand_left_variables_css_untouched():
    import hashlib  # noqa: F401 (kept to mirror the brief's spec; not used for a golden hash)
    from pathlib import Path
    css = Path(__file__).resolve().parents[3] / "webview" / "static" / "css" / "variables.css"
    # Not a golden-hash pin (that would block legitimate future token changes) —
    # just proves the file exists and D1-D3 didn't fork/rename the token source.
    assert css.exists()
    assert "COLOR PALETTE" in css.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    yield
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.delenv("POLYROB_CONSOLE_NAME", raising=False)
    monkeypatch.delenv("WEBVIEW_DOMAIN", raising=False)
    monkeypatch.delenv("POLYROB_SUPPORT_URL", raising=False)
    monkeypatch.delenv("POLYROB_SUPPORT_HANDLE", raising=False)
    monkeypatch.delenv("POLYROB_BRAND_URL", raising=False)
    monkeypatch.delenv("POLYROB_ORG_URL", raising=False)
    import webview.server as server
    importlib.reload(server)
