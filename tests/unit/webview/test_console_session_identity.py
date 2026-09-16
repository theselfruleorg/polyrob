"""Signed sessions for other identities must not become the console owner."""
import time
import uuid

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest


@pytest.fixture
def console(monkeypatch, tmp_path):
    from webview import server, webgate

    monkeypatch.setenv("JWT_SECRET_KEY", "console-identity-regression-secret-32")
    monkeypatch.setenv("TOKEN_DENYLIST_PATH", str(tmp_path / "revocations.db"))
    monkeypatch.setattr(webgate, "is_own_ops", lambda: True)
    monkeypatch.setattr(webgate, "requires_owner_login", lambda: True)
    monkeypatch.setattr(webgate, "local_owner_id", lambda: "bound-owner")
    monkeypatch.setattr("webview.session_access.may_read_session_path", lambda *args: True)
    app = FastAPI()
    app.middleware("http")(server.auth_middleware)

    @app.get("/api/webgate/wallet")
    def protected(request: Request):
        return {"user_id": request.state.user_id}

    return server, TestClient(app)


def _token(user_id, role="user"):
    return jwt.encode({"user_id": user_id, "role": role, "tier": "admin",
                       "exp": time.time() + 60, "jti": uuid.uuid4().hex},
                      "console-identity-regression-secret-32", algorithm="HS256")


@pytest.mark.parametrize("user_id", ["customer", "", None, 1, ["bound-owner"]])
@pytest.mark.parametrize("role", ["user", "owner", "admin"])
def test_own_ops_refuses_other_identity_on_every_decoder(console, user_id, role):
    server, client = console
    token = _token(user_id, role)
    response = client.get("/api/webgate/wallet", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    request = StarletteRequest({"type": "http", "path": "/", "headers": [(b"cookie", ("auth_token=" + token).encode())]})
    server._manual_auth_check(request)
    assert not getattr(request.state, "authenticated", False)
    assert server._decode_socket_payload(token) == {}


def test_own_ops_accepts_bound_owner_on_every_decoder(console):
    server, client = console
    token = _token("bound-owner", "owner")
    response = client.get("/api/webgate/wallet", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert response.json()["user_id"] == "bound-owner"
    request = StarletteRequest({"type": "http", "path": "/", "headers": [(b"cookie", ("auth_token=" + token).encode())]})
    server._manual_auth_check(request)
    assert request.state.authenticated and request.state.user_id == "bound-owner"
    assert server._decode_socket_payload(token)["user_id"] == "bound-owner"


def test_multitenant_still_accepts_customer_identity(console, monkeypatch):
    from webview import webgate
    server, client = console
    monkeypatch.setattr(webgate, "is_own_ops", lambda: False)
    token = _token("customer")
    response = client.get("/api/webgate/wallet", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert response.json()["user_id"] == "customer"
    assert server._decode_socket_payload(token)["user_id"] == "customer"


def test_owner_resolution_failure_refuses_session(console, monkeypatch):
    from webview import webgate
    server, client = console

    def unavailable():
        raise RuntimeError("owner unavailable")

    monkeypatch.setattr(webgate, "local_owner_id", unavailable)
    token = _token("bound-owner", "owner")
    assert client.get("/api/webgate/wallet", headers={"Authorization": "Bearer " + token}).status_code == 401
    assert server._decode_socket_payload(token) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "invalid", "other-owner"])
async def test_unauthorized_socket_handshake_is_rejected(console, token):
    server, _ = console
    if token == "other-owner":
        token = _token("customer", "owner")
    sid = "rejected-" + uuid.uuid4().hex
    assert await server.connect(sid, {}, auth={"token": token}) is False
    assert sid not in server._socket_user
    assert sid not in server._socket_auth_monitor.tasks
