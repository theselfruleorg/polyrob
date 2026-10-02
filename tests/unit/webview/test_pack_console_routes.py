"""The x pack's console routes on the REAL console app (own_ops posture).

The OAuth callback X redirects the owner's browser to is the ONE public pack
path: reachable with no session, a GET only, exact. Every other pack console
route stays behind the owner's session.
"""
import importlib

import pytest

pytest.importorskip("polyrob_x")

from argon2 import PasswordHasher
from fastapi import Response
from fastapi.testclient import TestClient

_ENV_KEYS = ("POLYROB_POSTURE", "WEBGATE_MULTITENANT", "WEBGATE_HOST", "JWT_SECRET_KEY",
             "POLYROB_OWNER_USERNAME", "POLYROB_OWNER_PASSWORD_HASH", "ENVIRONMENT",
             "POLYROB_OWNER_USER_ID", "WEBVIEW_READ_ONLY")

CALLBACK = "/api/packs/x/oauth/callback"
STATUS = "/api/packs/x/oauth/status"


@pytest.fixture
def srv(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", PasswordHasher().hash("s3cret"))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")      # prod's monitoring console
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.owner_auth as oa
    importlib.reload(oa)
    import webview.server as server
    importlib.reload(server)
    return server


def _owner_token():
    from webview.owner_auth import issue_owner_session_cookie
    return issue_owner_session_cookie(Response())


def test_the_callback_is_public_and_the_status_route_is_owner_only(srv):
    client = TestClient(srv._fastapi)
    r = client.get(CALLBACK, params={"state": "never-minted", "code": "c"},
                   follow_redirects=False)
    assert r.status_code == 400                           # reached the route, not 401/302
    assert "X login not renewed" in r.text and "unknown or was already used" in r.text
    assert "<script" not in r.text.lower()
    assert r.headers["Cache-Control"] == "no-store"
    # Exact and GET-only: no other method, no neighbour path.
    assert client.post(CALLBACK).status_code in (401, 403)
    assert client.get(CALLBACK + "/", follow_redirects=False).status_code in (401, 404)
    # Every other pack route needs the owner's session.
    assert client.get(STATUS).status_code == 401
    ok = client.get(STATUS, headers={"Authorization": f"Bearer {_owner_token()}"})
    assert ok.status_code == 200 and "oauth2" in ok.json()


def test_a_non_owner_token_is_refused(srv):
    import datetime
    import jwt
    token = jwt.encode({"sub": "t", "user_id": "tenant-9", "role": "user", "tier": "free",
                        "jti": "j", "exp": datetime.datetime.utcnow()
                        + datetime.timedelta(hours=1)}, "test-secret", algorithm="HS256")
    r = TestClient(srv._fastapi).get(STATUS, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (401, 403)


def test_a_completed_login_answers_the_page_and_notifies_the_owner(srv, monkeypatch):
    from polyrob_x import console_routes, x_login_flow
    seen = []

    async def _notify(owner, text):
        seen.append((owner, text))
    monkeypatch.setattr(console_routes, "_notify_owner", _notify)
    monkeypatch.setattr(x_login_flow, "complete_login", lambda state, code: x_login_flow.LoginResult(
        True, owner_user_id="owner-1", scope="dm.read tweet.read", expires_in=7100))
    r = TestClient(srv._fastapi).get(CALLBACK, params={"state": "s", "code": "c"})
    assert r.status_code == 200 and "X login renewed" in r.text
    assert "you can close this tab" in r.text
    assert seen and seen[0][0] == "owner-1" and seen[0][1].startswith("X login renewed")


def test_a_declined_login_burns_the_link_and_says_so(srv, monkeypatch):
    from polyrob_x import console_routes, x_login_flow
    seen, burned = [], []

    async def _notify(owner, text):
        seen.append((owner, text))
    monkeypatch.setattr(console_routes, "_notify_owner", _notify)
    monkeypatch.setattr(x_login_flow, "discard", lambda state: burned.append(state) or "owner-1")
    r = TestClient(srv._fastapi).get(CALLBACK, params={"state": "s", "error": "access_denied"})
    assert r.status_code == 400 and "access_denied" in r.text
    assert burned == ["s"] and "NOT renewed" in seen[0][1]
