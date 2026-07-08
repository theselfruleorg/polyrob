"""Item 1 reachability: the payable-invoice routes (/api/x402/requests/...) must be
reachable by an anonymous third-party payer. fallback_auth_middleware previously 401/503'd
every non-public /api/ path, so the whole invoice->pay loop could not be initiated.
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.app import fallback_auth_middleware


def _app():
    app = FastAPI()

    @app.get("/api/x402/requests/{rid}")
    async def challenge(rid: str):
        return {"ok": True, "rid": rid}

    @app.post("/api/x402/requests/{rid}/pay")
    async def pay(rid: str, request: Request):
        return {"paid": rid}

    @app.get("/api/other")
    async def other():
        return {"nope": True}

    app.middleware("http")(fallback_auth_middleware)
    return TestClient(app, raise_server_exceptions=False)


def test_invoice_challenge_reachable_anonymously(monkeypatch):
    # No API_AUTH_TOKEN, no auth headers — a real anonymous payer.
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    client = _app()
    r = client.get("/api/x402/requests/inv_abc")
    assert r.status_code == 200
    assert r.json()["rid"] == "inv_abc"


def test_invoice_pay_reachable_anonymously(monkeypatch):
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    client = _app()
    r = client.post("/api/x402/requests/inv_abc/pay")
    assert r.status_code == 200


def test_other_api_path_still_gated(monkeypatch):
    # Regression guard: opening the invoice prefix must not open the rest of /api/.
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    client = _app()
    r = client.get("/api/other")
    assert r.status_code in (401, 403, 503)  # still blocked
