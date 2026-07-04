"""Posture-routing invariants (design doc §1 'Required behaviors'), end-to-end
across webgate + server + owner_auth together — the cross-cutting contract the
narrower B1-B5 unit tests don't individually prove.

Posture 0 (local): full dashboard, no auth, no login surfaces at all.
Posture 1 (own_ops): minimal public status page on unauthenticated `/`,
owner-login gate present and unlocks the console, no SaaS UI.
Posture 2 (multitenant): SaaS signin mounted, owner-login also selectable.
"""
import importlib

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

_POSTURE_ENV_KEYS = (
    "POLYROB_POSTURE", "WEBGATE_MULTITENANT", "WEBGATE_HOST", "WEBGATE_PORT",
    "JWT_SECRET_KEY", "POLYROB_OWNER_USERNAME", "POLYROB_OWNER_PASSWORD_HASH",
    "ENVIRONMENT",
)


def _reload_webview():
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.owner_auth as oa
    importlib.reload(oa)
    import webview.server as srv
    importlib.reload(srv)
    return srv


def _client(monkeypatch, posture, owner_creds=False):
    monkeypatch.setenv("POLYROB_POSTURE", posture)
    if owner_creds:
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
        monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
        monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", PasswordHasher().hash("s3cret"))
        # The owner-login cookie is minted `secure=True` unless ENVIRONMENT !=
        # "production" (webview/owner_auth.py::issue_owner_session_cookie).
        # TestClient talks plain http://testserver, so a Secure cookie would be
        # silently dropped by the client's cookie jar on the next request —
        # not a server bug, a test-transport reality (see B4's
        # test_owner_login_route.py fixture for the same workaround).
        monkeypatch.setenv("ENVIRONMENT", "development")
    srv = _reload_webview()
    return TestClient(srv._fastapi)


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch):
    """Env leaks across webview test files (each test reloads the module-level
    singletons in webgate/owner_auth/server on live os.environ). Reset and
    reload after every test so later files in the same pytest session see a
    clean default posture again."""
    yield
    for k in _POSTURE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    _reload_webview()


class TestPosture0Local:
    def test_root_is_dashboard_no_auth(self, monkeypatch):
        c = _client(monkeypatch, "local")
        assert "POLYROB is live" not in c.get("/").text

    def test_no_owner_login_surface(self, monkeypatch):
        c = _client(monkeypatch, "local")
        assert c.get("/owner-login").status_code == 404

    def test_no_signin_surface(self, monkeypatch):
        c = _client(monkeypatch, "local")
        assert c.get("/signin").status_code == 404


class TestPosture1OwnOps:
    def test_root_unauthenticated_is_status_only(self, monkeypatch):
        c = _client(monkeypatch, "own_ops", owner_creds=True)
        resp = c.get("/")
        assert "POLYROB is live" in resp.text

    def test_root_unauthenticated_has_no_console_chrome(self, monkeypatch):
        c = _client(monkeypatch, "own_ops", owner_creds=True)
        resp = c.get("/")
        assert "settings" not in resp.text.lower() or "status-page" in resp.text.lower()

    def test_owner_login_gate_present(self, monkeypatch):
        c = _client(monkeypatch, "own_ops", owner_creds=True)
        assert c.get("/owner-login").status_code == 200

    def test_owner_login_unlocks_dashboard(self, monkeypatch):
        c = _client(monkeypatch, "own_ops", owner_creds=True)
        c.post("/owner-login", data={"username": "op", "password": "s3cret"})
        resp = c.get("/")
        assert "POLYROB is live" not in resp.text

    def test_no_saas_ui(self, monkeypatch):
        c = _client(monkeypatch, "own_ops", owner_creds=True)
        # /signin is unmounted (Posture-2-only, `_multitenant_get`) AND listed
        # in auth_middleware's public_paths allowlist, so an unauthenticated
        # request reaches the real router and gets a genuine 404.
        assert c.get("/signin").status_code == 404
        # /admin is unmounted too, but — unlike /signin — is NOT in
        # public_paths, so auth_middleware denies-and-redirects an
        # unauthenticated request to /owner-login BEFORE the request ever
        # reaches the router (same redirect-first pattern already precedented
        # by B4's test_posture_gating.py for /session/{id} and the
        # workspace/screenshot sub-routes: 302/303, never a bare 404, for any
        # protected path while unauthenticated). Assert on both signals: the
        # response is a denial (never a 200 with admin content when
        # unauthenticated), and following the redirect never renders the
        # admin dashboard's DOM (id="stats-grid" from admin/dashboard.html —
        # the invariant that actually matters per design doc §1.3).
        resp = c.get("/admin", follow_redirects=False)
        assert resp.status_code in (302, 303, 404)
        followed = c.get("/admin")
        assert 'id="stats-grid"' not in followed.text


class TestPosture2Multitenant:
    def test_saas_signin_mounted(self, monkeypatch):
        c = _client(monkeypatch, "multitenant")
        assert c.get("/signin").status_code == 200

    def test_owner_login_also_available(self, monkeypatch):
        c = _client(monkeypatch, "multitenant", owner_creds=True)
        assert c.get("/owner-login").status_code == 200
