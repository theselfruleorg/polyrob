"""Task 9 — owner-login hardening: per-IP throttle, CSRF, return_to sanitizing."""
import importlib
import re

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

_ENV_KEYS = (
    "POLYROB_POSTURE", "WEBGATE_MULTITENANT", "JWT_SECRET_KEY",
    "POLYROB_OWNER_USERNAME", "POLYROB_OWNER_PASSWORD_HASH", "ENVIRONMENT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def _own_ops_client(monkeypatch):
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", PasswordHasher().hash("s3cret"))
    monkeypatch.setenv("ENVIRONMENT", "development")
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.owner_auth as oa
    importlib.reload(oa)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


def _csrf_post(client, username, password, return_to="/", follow_redirects=True):
    """The legitimate flow: GET the form (mints csrf cookie+token), then POST."""
    page = client.get("/owner-login")
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text)
    token = match.group(1) if match else ""
    return client.post(
        "/owner-login",
        data={"username": username, "password": password,
              "return_to": return_to, "csrf_token": token},
        follow_redirects=follow_redirects,
    )


def test_sixth_attempt_throttled_429(monkeypatch):
    client = _own_ops_client(monkeypatch)
    for _ in range(5):
        resp = _csrf_post(client, "op", "wrong")
        assert resp.status_code == 401
    resp = _csrf_post(client, "op", "s3cret")  # even correct creds now throttled
    assert resp.status_code == 429


def test_post_without_csrf_token_403(monkeypatch):
    client = _own_ops_client(monkeypatch)
    resp = client.post("/owner-login", data={"username": "op", "password": "s3cret"})
    assert resp.status_code == 403


def test_csrf_flow_logs_in(monkeypatch):
    client = _own_ops_client(monkeypatch)
    resp = _csrf_post(client, "op", "s3cret", follow_redirects=False)
    assert resp.status_code == 303
    assert "auth_token" in resp.cookies


def test_return_to_open_redirect_neutralized(monkeypatch):
    client = _own_ops_client(monkeypatch)
    resp = _csrf_post(client, "op", "s3cret", return_to="https://evil.example",
                      follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    client2 = _own_ops_client(monkeypatch)
    resp2 = _csrf_post(client2, "op", "s3cret", return_to="//evil.example",
                       follow_redirects=False)
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/"
