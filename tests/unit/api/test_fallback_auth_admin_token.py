"""C4: the CONFIRMED bug — a valid API_AUTH_TOKEN caller gets tier="admin" but
NOT role="admin" from fallback_auth_middleware, so extract_admin_info() (which
defaults role to 'user') never grants the admin bypass, and the request falls
through to an unconditional 402 in verify_payment_for_request despite holding
a valid admin-tier token.

api/payment_verification.py:67 + api/app.py:593-594 (pre-fix).
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.app import fallback_auth_middleware
from api.payment_verification import verify_payment_for_request


class _FakeContainer:
    """Minimal container stand-in — DependencyContainer is a process-global
    singleton (core/container.py `_instance`) that raises ValueError if no
    test in the session has initialized it yet. Monkeypatching
    get_instance() (same pattern as test_payment_double_billing.py) keeps
    this test deterministic regardless of what else has run in the process."""

    config = None

    def get_service(self, name):
        return None


def _app():
    app = FastAPI()

    @app.post("/task/sessions")
    async def create_session(request: Request):
        method, details = await verify_payment_for_request(request, cost_credits=1)
        return {"payment_method": method, "details": details}

    app.middleware("http")(fallback_auth_middleware)
    return TestClient(app, raise_server_exceptions=False)


def test_valid_api_token_gets_admin_bypass_not_402(monkeypatch):
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token-123")
    from core.container import DependencyContainer
    monkeypatch.setattr(
        DependencyContainer, "get_instance",
        classmethod(lambda cls, *a, **k: _FakeContainer()),
    )
    client = _app()

    resp = client.post(
        "/task/sessions",
        json={"task": "hi"},
        headers={"X-API-KEY": "secret-token-123"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["payment_method"] == "admin_bypass"
