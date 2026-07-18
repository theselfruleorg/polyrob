"""Regression (G-19): GET /payments/deposit-address emitted a QR code URL
against `chart.googleapis.com` — Google's Charts QR endpoint was discontinued
years ago, so the link is dead. The field must stay on the response model
(API compatibility) but no longer point at a 404.
"""
import core.container as container_mod
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.payment_endpoints import router


class _FakeWalletGenerator:
    def generate_deposit_address(self, user_id):
        return "0xDEADBEEF00000000000000000000000000BEEF"


class _FakeDatabaseManager:
    async def fetch_one(self, *args, **kwargs):
        return None  # no existing deposit_address row

    async def execute(self, *args, **kwargs):
        return None


class _FakeContainer:
    def get_service(self, name):
        return {
            "wallet_generator": _FakeWalletGenerator(),
            "database_manager": _FakeDatabaseManager(),
        }.get(name)


def _client(user_id, monkeypatch):
    monkeypatch.setattr(
        container_mod.DependencyContainer, "get_instance",
        classmethod(lambda cls, *a, **k: _FakeContainer()),
    )
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.user_id = user_id
        request.state.role = "user"
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_deposit_address_qr_code_url_is_none(monkeypatch):
    c = _client("u_test", monkeypatch)
    resp = c.get("/payments/deposit-address")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["qr_code_url"] is None
    assert "chart.googleapis.com" not in resp.text


def test_deposit_address_response_still_has_the_field(monkeypatch):
    # Field stays on the model for API compatibility — just always None now.
    c = _client("u_test", monkeypatch)
    resp = c.get("/payments/deposit-address")
    assert "qr_code_url" in resp.json()
