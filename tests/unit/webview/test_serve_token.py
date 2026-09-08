"""030 S1 companion: short-lived HMAC serve-tokens for the workspace iframe.

Dropping ``allow-same-origin`` from the preview sandbox (the S1 fix — agent
HTML must never run with the owner's console origin) gives the iframe an
opaque origin, and an opaque-origin subframe no longer sends the SameSite
auth cookie. The authed page fetches a scoped, expiring token and appends it
as ``?st=`` so the /serve/ document still loads in own_ops — without ever
widening the sandbox back.
"""
import importlib
import time as _time

import pytest
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
    yield
    for k in _POSTURE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    _reload_webview()


def _srv(monkeypatch, posture="own_ops"):
    monkeypatch.setenv("POLYROB_POSTURE", posture)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENVIRONMENT", "development")
    return _reload_webview()


def test_mint_verify_roundtrip(monkeypatch):
    srv = _srv(monkeypatch)
    tok = srv._mint_serve_token("sess1")
    assert tok and srv._verify_serve_token("sess1", tok) is True


def test_token_is_session_scoped(monkeypatch):
    srv = _srv(monkeypatch)
    tok = srv._mint_serve_token("sess1")
    assert srv._verify_serve_token("other", tok) is False


def test_expired_token_is_refused(monkeypatch):
    srv = _srv(monkeypatch)
    real_time = _time.time
    monkeypatch.setattr(srv.time, "time", lambda: real_time() - 100000)
    tok = srv._mint_serve_token("sess1")
    monkeypatch.setattr(srv.time, "time", real_time)
    assert srv._verify_serve_token("sess1", tok) is False


def test_garbage_tokens_are_refused(monkeypatch):
    srv = _srv(monkeypatch)
    for bad in ("", "x", "123.deadbeef", None):
        assert srv._verify_serve_token("sess1", bad) is False


def test_middleware_admits_a_valid_serve_token_in_own_ops(monkeypatch):
    srv = _srv(monkeypatch)
    client = TestClient(srv._fastapi)
    tok = srv._mint_serve_token("sess1")
    # Without a token: 401/redirect. With: passes auth (404 = no such file,
    # which proves the request reached the route handler).
    bare = client.get("/api/session/sess1/workspace/serve/x.html",
                      follow_redirects=False)
    assert bare.status_code in (401, 302, 303)
    with_tok = client.get(f"/api/session/sess1/workspace/serve/x.html?st={tok}",
                          follow_redirects=False)
    assert with_tok.status_code == 404


def test_middleware_refuses_a_token_for_another_session(monkeypatch):
    srv = _srv(monkeypatch)
    client = TestClient(srv._fastapi)
    tok = srv._mint_serve_token("other-session")
    r = client.get(f"/api/session/sess1/workspace/serve/x.html?st={tok}",
                   follow_redirects=False)
    assert r.status_code in (401, 302, 303)


def test_serve_token_endpoint_requires_auth(monkeypatch):
    srv = _srv(monkeypatch)
    client = TestClient(srv._fastapi)
    r = client.get("/api/session/sess1/workspace/serve-token",
                   follow_redirects=False)
    assert r.status_code in (401, 302, 303)
