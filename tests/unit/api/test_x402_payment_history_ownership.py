"""Regression (P1): GET /api/x402/payment-history/{wallet_address} returned ANY
wallet's full payment history by (guessable) address with no ownership check. A
wallet maps deterministically to a user_id, so the caller must own the wallet.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.x402_endpoints import router
from modules.x402 import generate_user_id_from_wallet


def _client(user_id=None):
    app = FastAPI()
    if user_id is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):
            request.state.user_id = user_id
            return await call_next(request)
    app.include_router(router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


WALLET = "0xabcdef0000000000000000000000000000abcd"


def test_unauthenticated_is_denied():
    c = _client(user_id=None)
    resp = c.get(f"/api/x402/payment-history/{WALLET}")
    assert resp.status_code == 401


def test_non_owner_is_forbidden():
    c = _client(user_id="usr_someone_else")
    resp = c.get(f"/api/x402/payment-history/{WALLET}")
    assert resp.status_code == 403


def test_owner_passes_the_ownership_gate():
    owner_id = generate_user_id_from_wallet(WALLET.lower())
    c = _client(user_id=owner_id)
    resp = c.get(f"/api/x402/payment-history/{WALLET}")
    # Past the gate: never 401/403. (May 503 if no DB is wired in this bare app.)
    assert resp.status_code not in (401, 403)
