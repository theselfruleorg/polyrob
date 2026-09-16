"""043 A12 — an unconfigured console must not show dead footer links.

``webgate.branding_config()`` used to default ``brand_url``/``org_url`` (and the
``terms``/``privacy`` URLs derived from ``brand_url``) to a placeholder host
(``https://your-polyrob-host.example``) or the framework author's own org site
(``https://theselfrule.org``) when the operator never set the corresponding
env var. Every page rendered those as real ``<a>`` footer links, so a fresh,
unconfigured deploy shipped three dead links on every page. The fix: an unset
env var renders as an empty string, and ``layout.html`` only emits a footer
link when its URL is non-empty.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, user_id="u1"):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id, raising=False)
    app = FastAPI()
    app.include_router(pages.router)
    # 043 phase 5: /pending is a normal route on pages.router now (the WEBVIEW_UI
    # legacy switch and webview.legacy were removed), so including the router is
    # all it takes to render it.
    return TestClient(app)


def _clear_branding_env(monkeypatch):
    for k in (
        "POLYROB_BRAND_URL",
        "POLYROB_ORG_URL",
        "POLYROB_TERMS_URL",
        "POLYROB_PRIVACY_URL",
    ):
        monkeypatch.delenv(k, raising=False)


def test_branding_config_returns_empty_strings_when_unset(monkeypatch):
    _clear_branding_env(monkeypatch)
    from webview import webgate

    cfg = webgate.branding_config()
    assert cfg["brand_url"] == ""
    assert cfg["org_url"] == ""
    assert cfg["terms_url"] == ""
    assert cfg["privacy_url"] == ""
    # support_url keeps its real Telegram default — not a placeholder host.
    assert cfg["support_url"] == "https://t.me/tmachinrobot"


def test_footer_hides_unset_links(monkeypatch, tmp_path):
    _clear_branding_env(monkeypatch)
    client = _client(monkeypatch, tmp_path)

    resp = client.get("/pending")
    assert resp.status_code == 200
    body = resp.text
    assert "your-polyrob-host.example" not in body
    assert "theselfrule.org" not in body
    # No terms/privacy footer-link anchors at all when both are unset.
    assert 'href="">' not in body


def test_footer_shows_terms_link_when_set(monkeypatch, tmp_path):
    _clear_branding_env(monkeypatch)
    monkeypatch.setenv("POLYROB_TERMS_URL", "https://example.com/terms")
    client = _client(monkeypatch, tmp_path)

    resp = client.get("/pending")
    assert resp.status_code == 200
    assert "https://example.com/terms" in resp.text
    assert "Terms of Use" in resp.text
