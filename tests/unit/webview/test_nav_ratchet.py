"""043 C8 / phase 5 — fifteen destinations became five, and the old ones are gone.

The console presented FIFTEEN flat top-level links in one nav bar. The new
information architecture is five (New · Inbox · Work · Money · Agent). Phase 5
removed the ``WEBVIEW_UI`` switch and the legacy pages entirely, so the new
console is the ONLY console. This ratchet pins the cut:

- the rendered shell carries at most five primary nav entries and none of the
  old top-level links;
- ``/sessions`` (the old dashboard, replaced by the Chats overlay) is gone —
  a 404;
- ``/pending`` (the approve/reject-quarantine page) survives as a normal route;
- the ``/api/webgate/*`` readers and the Inbox decision routes are always
  registered — the new pages read exactly those.

⚠️ ``/`` is served by the new chat shell; on ``own_ops``/``multitenant`` it is
also the public status page for an unauthenticated visitor
(``pages_new._public_visitor``).
"""
import importlib
import re

import pytest
from fastapi.testclient import TestClient

#: The old nav bar's top-level links that no longer appear. /sessions is deleted
#: (043 §9 phase 5, replaced by the Chats overlay); the memory/knowledge/
#: autonomy/finance/positions/apps/preferences/config/identity/system/settings
#: pages were deleted in phases 3-4. /pending survives as a normal route but is
#: not a top-level nav link.
OLD_NAV_LINKS = ["/sessions", "/memory", "/autonomy", "/finance", "/positions",
                 "/apps", "/knowledge", "/preferences", "/config", "/identity",
                 "/system", "/settings"]

_NAV_HREF = re.compile(r'<a[^>]*class="[^"]*nav-(?:link|item)[^"]*"[^>]*href="([^"]+)"'
                       r'|<a[^>]*href="([^"]+)"[^>]*class="[^"]*nav-(?:link|item)[^"]*"')


def _client(monkeypatch):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("ENV", "development")
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi), srv


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    for key in ("POLYROB_POSTURE", "ENV"):
        monkeypatch.delenv(key, raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)


def _nav_hrefs(html: str):
    return [a or b for a, b in _NAV_HREF.findall(html)]


def test_the_shell_has_at_most_five_primary_nav_entries(monkeypatch):
    client, _srv = _client(monkeypatch)
    hrefs = _nav_hrefs(client.get("/").text)
    assert len(hrefs) <= 5, hrefs


def test_the_shell_shows_none_of_the_old_top_level_links(monkeypatch):
    client, _srv = _client(monkeypatch)
    hrefs = set(_nav_hrefs(client.get("/").text))
    assert hrefs.isdisjoint(OLD_NAV_LINKS), sorted(hrefs & set(OLD_NAV_LINKS))


def test_the_legacy_dashboard_is_gone(monkeypatch):
    """The old /sessions dashboard is deleted — the Chats overlay replaces it."""
    client, _srv = _client(monkeypatch)
    assert client.get("/sessions").status_code == 404


def test_pending_survives_as_a_normal_route(monkeypatch):
    """/pending is the one surviving webgate page (043 phase 5)."""
    client, _srv = _client(monkeypatch)
    assert client.get("/pending").status_code == 200


def test_the_json_endpoints_are_registered(monkeypatch):
    """The new pages READ these; they were never gated by the page switch."""
    client, _srv = _client(monkeypatch)
    for path in ("/api/webgate/doctor", "/api/webgate/goals",
                 "/api/webgate/cron", "/api/webgate/pause",
                 "/api/activity/backfill", "/api/webgate/apps",
                 "/api/webgate/inbox"):
        assert client.get(path).status_code != 404, path


def test_the_inbox_decision_routes_are_registered(monkeypatch):
    """A reader without its writers is a page that can show a decision and not
    take it."""
    client, _srv = _client(monkeypatch)
    for verb in ("decide", "reject", "fulfill"):
        resp = client.post(f"/api/webgate/inbox/ask/nope/{verb}")
        assert resp.status_code != 404, verb


def test_the_index_is_served(monkeypatch):
    """`/` is the new chat shell (and the public status page for a stranger)."""
    client, _srv = _client(monkeypatch)
    assert client.get("/").status_code == 200
