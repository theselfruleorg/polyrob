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


@pytest.mark.parametrize("path", ["/", "/sessions", "/settings"])
def test_local_pages_hide_tenant_nav(monkeypatch, path):
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    html = r.text
    assert 'href="/signin"' not in html, f"{path} leaks Sign In link in local posture"
    assert 'href="/profile"' not in html, f"{path} leaks Profile link in local posture"


@pytest.mark.parametrize("path", ["/memory", "/autonomy", "/identity", "/system"])
def test_local_webgate_pages_still_hide_tenant_nav(monkeypatch, path):
    """The webgate pages pass is_multitenant explicitly — must stay hidden."""
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    html = client.get(path).text
    assert 'href="/signin"' not in html
    assert 'href="/profile"' not in html


def test_multitenant_page_keeps_tenant_nav(monkeypatch):
    """Multitenant posture must keep the tenant nav on a layout-extending page.

    ``/session/{id}`` is public (view-only) in multitenant and extends
    layout.html WITHOUT passing ``is_multitenant`` — the posture-global default
    must show the tenant links there.
    """
    server = _reload_server(monkeypatch, multitenant=True)
    client = TestClient(server._fastapi)
    r = client.get("/session/nav-posture-test")
    assert r.status_code == 200
    html = r.text
    assert 'href="/profile"' in html
    assert 'href="/signin"' in html
