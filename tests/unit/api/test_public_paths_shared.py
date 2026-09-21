"""B1: ONE public-path allow-list, honoured by BOTH HTTP auth gates.

`AuthenticationMiddleware` kept five EXACT strings while
`fallback_auth_middleware` kept a different prefix list, so the moment
`API_SECRET`/`ADMIN_TOKEN` was set every documented-public route 401'd:
`/api/auth/nonce` (the login handshake that MINTS the credential),
`/.well-known/agent.json`, `/api/x402/*`, `/api/pricing/*`, `/webhooks/*`.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth_constants import is_public_path
from api.app import fallback_auth_middleware
from api.middleware import AuthenticationMiddleware


#: Every path the product documents as reachable without an account.
DOCUMENTED_PUBLIC_PATHS = [
    "/",
    "/health",
    "/docs",
    "/openapi.json",
    "/api/auth/nonce",
    "/api/auth/verify",
    "/api/x402/pricing",
    "/api/x402/requests/abc123",
    "/api/pricing/models",
    "/webhooks/telegram",
    "/.well-known/agent.json",
    "/eip8004/registration.json",
]


@pytest.mark.parametrize("path", DOCUMENTED_PUBLIC_PATHS)
def test_documented_public_paths_are_public(path):
    assert is_public_path(path), f"{path} must not require authentication"


@pytest.mark.parametrize("path", [
    "/api/task/sessions",
    "/api/admin/users",
    "/api/payments/balance",
    "/a2a/rpc",
    "/v1/chat/completions",
])
def test_private_paths_are_not_public(path):
    assert not is_public_path(path)


def test_root_is_matched_exactly_never_as_a_prefix():
    """'/' as a startswith() entry matches EVERY path — a total auth bypass."""
    assert is_public_path("/")
    assert not is_public_path("/api/admin/users")


def _app_with_auth_middleware():
    app = FastAPI()

    @app.get("/{full_path:path}")
    async def catch_all(full_path: str):
        return {"ok": True}

    app.add_middleware(AuthenticationMiddleware, secret_key="x" * 40)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", DOCUMENTED_PUBLIC_PATHS)
def test_auth_middleware_lets_every_documented_public_path_through(path):
    """With API_SECRET set (the middleware mounted), a public path is 200."""
    client = _app_with_auth_middleware()
    resp = client.get(path)
    assert resp.status_code != 401, f"{path} 401'd with the auth middleware on"


def test_auth_middleware_still_refuses_a_private_path():
    client = _app_with_auth_middleware()
    assert client.get("/api/task/sessions").status_code == 401


@pytest.mark.parametrize("path", DOCUMENTED_PUBLIC_PATHS)
def test_fallback_middleware_lets_every_documented_public_path_through(
        path, monkeypatch):
    """The fallback gate 503s when API_AUTH_TOKEN is unset — a public path must
    never reach it."""
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    app = FastAPI()

    @app.get("/{full_path:path}")
    async def catch_all(full_path: str):
        return {"ok": True}

    app.middleware("http")(fallback_auth_middleware)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(path)
    assert resp.status_code == 200, f"{path}: {resp.text}"
