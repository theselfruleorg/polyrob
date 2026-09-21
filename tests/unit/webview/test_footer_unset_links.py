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
#: A surviving ``layout.html`` page. ⚠️ A21 (2026-09-21) deleted ``/pending``,
#: the last layout.html page with an HTTP route in the single-user posture, so
#: the footer rule — which lives in ``layout.html`` — is exercised by rendering
#: one of its pages through the console's OWN template engine (the same move
#: ``test_render_smoke`` makes). The subject is the SHELL, not a page.
_LAYOUT_TEMPLATE = "status.html"


def _layout_html(monkeypatch) -> str:
    import importlib

    import webview.pages as pages
    importlib.reload(pages)  # branding globals are read at register time
    return pages._TEMPLATES.get_template(_LAYOUT_TEMPLATE).render(
        request=None, instance_id="polyrob", version="0.0.0-test")


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


def test_footer_hides_unset_links(monkeypatch):
    _clear_branding_env(monkeypatch)
    body = _layout_html(monkeypatch)
    assert body.strip(), "the layout rendered nothing"
    assert "your-polyrob-host.example" not in body
    assert "theselfrule.org" not in body
    # No terms/privacy footer-link anchors at all when both are unset.
    assert 'href="">' not in body


def test_footer_shows_terms_link_when_set(monkeypatch):
    _clear_branding_env(monkeypatch)
    monkeypatch.setenv("POLYROB_TERMS_URL", "https://example.com/terms")
    body = _layout_html(monkeypatch)
    assert "https://example.com/terms" in body
    assert "Terms of Use" in body
