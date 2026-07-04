import importlib
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def own_ops_owner_client(monkeypatch):
    from argon2 import PasswordHasher
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", PasswordHasher().hash("s3cret"))
    # The owner-login cookie is minted with `secure=True` unless ENVIRONMENT !=
    # "production" (webview/owner_auth.py::issue_owner_session_cookie). FastAPI's
    # TestClient talks plain http://testserver, so a Secure-flagged cookie is
    # silently dropped by the client's own cookie jar on the next request —
    # not a server bug, just a test-transport reality. Use "development" here
    # so the round-trip test below actually exercises the cookie read-back
    # instead of accidentally passing for an unrelated reason (e.g. a redirect
    # response with no body also satisfies a weak "text not in body" check).
    monkeypatch.setenv("ENVIRONMENT", "development")
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.owner_auth as oa
    importlib.reload(oa)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


def test_owner_login_page_reachable_unauthenticated(own_ops_owner_client):
    resp = own_ops_owner_client.get("/owner-login")
    assert resp.status_code == 200


def test_owner_login_wrong_password_401(own_ops_owner_client):
    resp = own_ops_owner_client.post(
        "/owner-login", data={"username": "op", "password": "wrong"},
    )
    assert resp.status_code == 401


def test_owner_login_success_sets_cookie_and_redirects(own_ops_owner_client):
    resp = own_ops_owner_client.post(
        "/owner-login", data={"username": "op", "password": "s3cret"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert "auth_token" in resp.cookies


def test_owner_login_then_root_shows_dashboard(own_ops_owner_client):
    """B4: auth_middleware/_manual_auth_check now short-circuit on
    webgate.requires_owner_login() rather than is_multitenant(), so own_ops
    requests reach the JWT-decode branch that populates
    request.state.authenticated from the owner cookie this route mints —
    the "owner logs in -> dashboard" round-trip works end to end."""
    login = own_ops_owner_client.post(
        "/owner-login", data={"username": "op", "password": "s3cret"},
    )
    assert login.status_code == 200  # after following the redirect
    root = own_ops_owner_client.get("/")
    assert root.status_code == 200
    assert "POLYROB is live" not in root.text  # dashboard, not the status page
    # Positive confirmation this is actually the dashboard (session.html),
    # not merely "some other page that happens to lack that string" — mirrors
    # test_public_status.py's own-ops-unauthenticated DOM-marker check.
    assert 'id="chat-input"' in root.text
