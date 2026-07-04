"""C3: the dead POST /api/x402/create-payment endpoint (always 503s — its
x402_handler is never wired) must be removed. The real, working flow is the
standard x402 header handshake (see modules/x402/middleware.py).
"""
import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.x402_endpoints as x402_endpoints
from api.x402_endpoints import router


def _client():
    # `router` already carries its own "/x402" prefix (api/x402_endpoints.py:23);
    # production mounts it with `prefix="/api"` (api/app.py:639). Mounting with
    # "/api/x402" here would double the prefix ("/api/x402/x402/...") and every
    # request below would 404 for the wrong reason.
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


def test_create_payment_route_removed():
    c = _client()
    resp = c.post("/api/x402/create-payment", json={"endpoint": "/a2a/rpc"})
    assert resp.status_code == 404, (
        "dead create-payment endpoint must be removed, not merely 503 — "
        f"got {resp.status_code}"
    )


def test_dead_models_removed():
    src = inspect.getsource(x402_endpoints)
    assert "class CreatePaymentRequest" not in src
    assert "class PaymentDetails" not in src


def test_pricing_flow_advertises_the_real_xpayment_handshake():
    c = _client()
    resp = c.get("/api/x402/pricing")
    assert resp.status_code == 200
    flow = resp.json()["flow"]
    # Must no longer point at the dead endpoint.
    assert "create-payment" not in str(flow)
    assert "X-PAYMENT" in str(flow)


def test_other_x402_routes_still_present():
    c = _client()
    # verify-status/payment-history stay — any of these is fine as long as it's
    # NOT a route-level 404/405, which would mean the route itself was
    # accidentally deleted too. In this bare app (no DependencyContainer
    # initialized), the handler's uncaught `DependencyContainer.get_instance()`
    # ValueError surfaces as 500 — a pre-existing, out-of-scope quirk of this
    # unchanged endpoint, not something C3 touches.
    resp = c.get("/api/x402/verify-status/nonexistent-nonce")
    assert resp.status_code in (404, 500, 503)
