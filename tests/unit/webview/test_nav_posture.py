"""P0-3 (2026-07-06 UX handoff) — tenant nav must not leak into single-user postures.

``layout.html`` used to treat an UNDEFINED ``is_multitenant`` as true, so every
server.py-rendered page (``/``, ``/sessions``, ``/settings`` — which don't pass
the variable) showed Profile / Sign In links in ``local``/``own_ops``. The
layout now defaults to the posture SSOT via the ``is_multitenant_posture``
Jinja global (``webgate.is_multitenant``), with an explicitly-passed
``is_multitenant`` variable still winning.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


def _reload_server(monkeypatch, multitenant=False):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true" if multitenant else "false")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    return importlib.reload(server)


# 043 phase 5: /sessions is deleted (the Chats overlay replaces it), so `/` (now
# the new chat shell) is the server.py-rendered root that must not leak tenant nav.
@pytest.mark.parametrize("path", ["/"])
def test_local_pages_hide_tenant_nav(monkeypatch, path):
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    html = r.text
    assert 'href="/signin"' not in html, f"{path} leaks Sign In link in local posture"
    assert 'href="/profile"' not in html, f"{path} leaks Profile link in local posture"


# 043 §9 phase 4: /memory, /identity, /system pages deleted; /pending is the
# surviving legacy webgate page that passes is_multitenant explicitly.
@pytest.mark.parametrize("path", ["/pending"])
def test_local_webgate_pages_still_hide_tenant_nav(monkeypatch, path):
    """The webgate pages pass is_multitenant explicitly — must stay hidden."""
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    html = client.get(path).text
    assert 'href="/signin"' not in html
    assert 'href="/profile"' not in html


# The multitenant tenant-nav "show" case was pinned via ``/session/{id}`` — the
# one public (view-only, no-auth) layout.html page in multitenant. 043 §9 deletes
# the legacy session.html view and turns ``/session/{id}`` into a 301 to /c/{id}
# (that public redirect is covered by
# test_posture_gating::test_session_page_still_public_in_multitenant), so there
# is no public layout.html page left to assert the tenant nav on. The hide case
# stays covered by test_local_pages_hide_tenant_nav above; the tenant-nav default
# is a legacy layout.html concern the new shell (shell.html + pages_new.NAV)
# replaces, and phase 5 removes the legacy pages entirely.
