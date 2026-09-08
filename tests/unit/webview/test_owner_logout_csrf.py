"""Proposal 030 S5 — webview owner-login flow: own_ops logout + CSRF re-mint.

Two live bugs (webview/server.py):
1. ``/logout`` was registered ``_multitenant_get`` (multitenant-only), so the
   own_ops posture — the prod shape — had NO way to end the 7-day owner
   session (the route 404'd). It is now posture-gated to own_ops AND
   multitenant, and own_ops answers with a real redirect + ``delete_cookie``
   (never a 200 JS-hack page — the auth middleware's own rule).
2. Every failure path of ``owner_login_submit`` re-rendered owner_login.html
   with ``csrf_token=None``; the template omits the hidden field then, so the
   NEXT attempt always failed CSRF with 403 — one typo'd password bricked the
   form. Every render now mints a fresh nonce cookie + matching token.
"""
import importlib
import re

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

_POSTURE_ENV_KEYS = (
    "POLYROB_POSTURE", "WEBGATE_MULTITENANT", "JWT_SECRET_KEY",
    "POLYROB_OWNER_USERNAME", "POLYROB_OWNER_PASSWORD_HASH", "ENVIRONMENT",
)


def _reload_webview():
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.owner_auth as oa
    importlib.reload(oa)
    import webview.server as srv
    importlib.reload(srv)
    return srv


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch):
    """Env leaks across webview test files (module-level singletons reload on
    live os.environ) — reset and reload after every test so later files see a
    clean default posture again (same pattern as test_posture_routing.py)."""
    yield
    for k in _POSTURE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    _reload_webview()


def _client(monkeypatch, posture):
    monkeypatch.setenv("POLYROB_POSTURE", posture)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", PasswordHasher().hash("s3cret"))
    # The owner-login cookie is minted `secure=True` unless ENVIRONMENT !=
    # "production" (webview/owner_auth.py::issue_owner_session_cookie).
    # TestClient talks plain http://testserver, so a Secure cookie would be
    # silently dropped by the client's cookie jar on the next request — not a
    # server bug, a test-transport reality (see test_owner_login_route.py).
    monkeypatch.setenv("ENVIRONMENT", "development")
    srv = _reload_webview()
    return TestClient(srv._fastapi)


@pytest.fixture
def own_ops_client(monkeypatch):
    return _client(monkeypatch, "own_ops")


@pytest.fixture
def multitenant_client(monkeypatch):
    return _client(monkeypatch, "multitenant")


_CSRF_FIELD = re.compile(r'name="csrf_token" value="([0-9a-f]+)"')


def _login_post(client, username, password, follow_redirects=True):
    """Legitimate login flow: GET mints the CSRF cookie+token, then POST."""
    page = client.get("/owner-login")
    match = _CSRF_FIELD.search(page.text)
    token = match.group(1) if match else ""
    return client.post(
        "/owner-login",
        data={"username": username, "password": password, "csrf_token": token},
        follow_redirects=follow_redirects,
    )


# --- (a) own_ops /logout ---------------------------------------------------- #

def test_own_ops_logout_clears_cookie_and_redirects(own_ops_client):
    login = _login_post(own_ops_client, "op", "s3cret", follow_redirects=False)
    assert login.status_code in (302, 303)
    assert own_ops_client.cookies.get("auth_token")

    resp = own_ops_client.get("/logout", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == "/owner-login"
    set_cookie = resp.headers.get("set-cookie", "")
    assert "auth_token=" in set_cookie and "Max-Age=0" in set_cookie
    # A real redirect, not a 200 JS-hack page (auth middleware rule).
    assert "<script>" not in (resp.text or "")
    # The client jar dropped the cookie...
    assert not own_ops_client.cookies.get("auth_token")
    # ...and a protected page now bounces to the login gate again.
    after = own_ops_client.get("/memory", follow_redirects=False)
    assert after.status_code in (302, 303)
    assert after.headers["location"].startswith("/owner-login")


def test_own_ops_logged_in_page_shows_logout_link(own_ops_client):
    _login_post(own_ops_client, "op", "s3cret")
    root = own_ops_client.get("/")
    assert root.status_code == 200
    assert 'href="/logout"' in root.text
    # Tenant-only links must still stay hidden in own_ops.
    assert 'href="/signin"' not in root.text
    assert 'href="/profile"' not in root.text


def test_own_ops_logged_out_page_has_no_logout_link(own_ops_client):
    page = own_ops_client.get("/owner-login")
    assert page.status_code == 200
    assert 'href="/logout"' not in page.text


# --- (b) failed login keeps the CSRF field ---------------------------------- #

def test_failed_login_rerender_keeps_csrf_field(own_ops_client):
    resp = _login_post(own_ops_client, "op", "wrong")
    assert resp.status_code == 401
    match = _CSRF_FIELD.search(resp.text)
    assert match and match.group(1), "401 re-render lost the csrf_token hidden field"

    # Second attempt using the re-rendered token + re-minted nonce cookie
    # succeeds — the form is NOT bricked.
    retry = own_ops_client.post(
        "/owner-login",
        data={"username": "op", "password": "s3cret", "csrf_token": match.group(1)},
        follow_redirects=False,
    )
    assert retry.status_code in (302, 303)
    assert "auth_token" in retry.cookies


def test_csrf_reject_rerender_recovers(own_ops_client):
    own_ops_client.get("/owner-login")  # mint a nonce cookie
    resp = own_ops_client.post(
        "/owner-login",
        data={"username": "op", "password": "s3cret", "csrf_token": "bogus"},
    )
    assert resp.status_code == 403
    match = _CSRF_FIELD.search(resp.text)
    assert match and match.group(1), "403 re-render lost the csrf_token hidden field"

    retry = own_ops_client.post(
        "/owner-login",
        data={"username": "op", "password": "s3cret", "csrf_token": match.group(1)},
        follow_redirects=False,
    )
    assert retry.status_code in (302, 303)


# --- (c) multitenant logout unchanged --------------------------------------- #

def test_multitenant_logout_still_works(multitenant_client):
    resp = multitenant_client.get("/logout")
    assert resp.status_code == 200
    # Legacy behavior kept: an HTML page that clears localStorage (the
    # wallet/SIWE JWT lives there in multitenant) and JS-redirects to /signin…
    assert "localStorage.clear" in resp.text
    assert "/signin" in resp.text
    # …while the server still deletes the cookie.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "auth_token=" in set_cookie and "Max-Age=0" in set_cookie
