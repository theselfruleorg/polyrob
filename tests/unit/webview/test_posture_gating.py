"""Posture-aware route gating (B4).

Reworks the old binary ``is_multitenant()`` gates on three seams
(``_multitenant_get`` mount-gate, ``auth_middleware`` short-circuit,
``public_paths`` exemptions) to branch on the full posture model
(``local`` | ``own_ops`` | ``multitenant``) instead. Also closes assessment
gaps 2/3: ``/session/`` and ``/api/session/`` (which covers the
workspace-file/screenshot sub-routes) are no longer unconditionally public —
``own_ops`` has no legitimate shareable-link use case (single owner, no
identity model for "someone who isn't the owner"); ``multitenant`` keeps the
existing shareable-link behavior unchanged.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def local_client(monkeypatch):
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


@pytest.fixture
def own_ops_client(monkeypatch):
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


@pytest.fixture
def multitenant_client(monkeypatch):
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true")
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


def test_signin_route_not_mounted_in_local(local_client):
    assert local_client.get("/signin").status_code == 404


def test_signin_route_mounted_in_multitenant(multitenant_client):
    resp = multitenant_client.get("/signin")
    assert resp.status_code == 200


def test_owner_login_route_mounted_in_own_ops(own_ops_client):
    assert own_ops_client.get("/owner-login").status_code == 200


def test_owner_login_route_mounted_in_multitenant_too(multitenant_client):
    # wallet sign-in stays primary in Posture 2, but owner login is "optionally
    # also selectable" per the design doc §1 — route must still exist.
    assert multitenant_client.get("/owner-login").status_code == 200


def test_owner_login_route_NOT_mounted_in_local(local_client):
    # Posture 0 has no auth at all — no login surface needed or wanted.
    assert local_client.get("/owner-login").status_code == 404


def test_owner_login_post_route_NOT_mounted_in_local(local_client):
    resp = local_client.post("/owner-login", data={"username": "x", "password": "y"})
    assert resp.status_code == 404


def test_session_page_requires_auth_in_own_ops(own_ops_client):
    # Gap 2 fix: /session/{id} viewing is no longer unconditionally public.
    resp = own_ops_client.get("/session/some-session-id", follow_redirects=False)
    assert resp.status_code in (302, 303, 401)


def test_session_page_public_in_local(local_client):
    # Posture 0 unregressed: local viewing stays open (no auth exists at all).
    resp = local_client.get("/session/some-session-id")
    assert resp.status_code in (200, 404)  # 404 only if the session dir doesn't exist


def test_workspace_file_route_requires_auth_in_own_ops(own_ops_client):
    resp = own_ops_client.get(
        "/api/session/some-session-id/workspace/file", follow_redirects=False
    )
    assert resp.status_code in (302, 303, 401)


def test_screenshot_route_requires_auth_in_own_ops(own_ops_client):
    # Gap 3: screenshot routes live under /api/session/{id}/... too.
    resp = own_ops_client.get(
        "/api/session/some-session-id/screenshot", follow_redirects=False
    )
    assert resp.status_code in (302, 303, 401)


def test_session_page_still_public_in_multitenant(multitenant_client):
    # Existing shareable-link behavior is UNCHANGED in multitenant.
    resp = multitenant_client.get("/session/some-session-id", follow_redirects=False)
    assert resp.status_code == 200


def test_unauthenticated_own_ops_redirect_targets_owner_login(own_ops_client):
    resp = own_ops_client.get("/session/some-session-id", follow_redirects=False)
    if resp.status_code in (302, 303):
        assert "/owner-login" in resp.headers.get("location", "")


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    yield
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
