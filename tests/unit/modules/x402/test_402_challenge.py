"""C7: the x402 middleware must issue a real 402 challenge (machine-readable
payment requirements) for a genuinely-unauthenticated request to a gated
endpoint, instead of silently passing it through to call_next (today's gap).

Credit-paying (Authorization/X-API-KEY) requests must NOT be touched here —
they still flow through to the existing downstream verify_payment_for_request.

IMPORTANT: this test mounts the REAL a2a + openai-compat routers at their
REAL prefixes (exactly as api/app.py does: api.a2a.endpoints.router and
api.a2a.streaming.router are prefix="/a2a", api.openai_compat.router.router
declares "/v1/chat/completions"/"/v1/models" inline) rather than a synthetic
bare route. That is deliberate: a prior version of this test used a fake
`@app.post("/task/sessions")` route, which passed even though the real
allowlist entry ("/task/sessions") was both mis-prefixed (real mount is
"/api/task/sessions") and pre-empted by fallback_auth_middleware — i.e. the
challenge was dead code in production while the test stayed green. Exercising
the real routers means a future prefix mismatch on a gated path fails here.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.x402.middleware import X402PaymentMiddleware
from api.a2a.endpoints import router as a2a_router
from api.a2a.streaming import router as a2a_streaming_router
from api.openai_compat.router import router as openai_compat_router


def _app():
    app = FastAPI()
    # Real routers, real prefixes — same routers api/app.py mounts.
    app.include_router(a2a_router)
    app.include_router(a2a_streaming_router)
    app.include_router(openai_compat_router)
    app.add_middleware(X402PaymentMiddleware, enabled=True)
    return TestClient(app, raise_server_exceptions=False)


def test_anonymous_request_to_real_gated_a2a_rpc_gets_402_challenge(monkeypatch):
    """/a2a/rpc is a genuinely reachable gated path: real mount, not pre-empted
    by fallback_auth_middleware (that middleware only gates /api/ and /task/)."""
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("X402_PRICE_USD", "0.02")
    client = _app()

    resp = client.post("/a2a/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/list", "params": {}})

    assert resp.status_code == 402
    body = resp.json()
    accepts = body["accepts"][0]
    assert accepts["payTo"] == "0x" + "1" * 40
    assert accepts["network"] == "base"
    assert accepts["maxAmountRequired"] == str(int(0.02 * 10 ** 6))  # USDC, 6 decimals
    # price parity (C2): the challenge amount must match the SSOT price.
    assert body["amount_usd"] == 0.02


def test_anonymous_request_to_real_gated_v1_chat_completions_gets_402_challenge(monkeypatch):
    """/v1/chat/completions (openai-compat) is the other real gated surface."""
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("X402_PRICE_USD", "0.02")
    client = _app()

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 402
    body = resp.json()
    assert body["amount_usd"] == 0.02


def test_real_ungated_v1_models_path_not_challenged(monkeypatch):
    """/v1/models is a REAL mounted route on the same router, but it is not in
    the gated allowlist (only /v1/chat/completions is) — must never be 402'd."""
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    client = _app()

    resp = client.get("/v1/models")

    assert resp.status_code != 402


def test_request_with_bearer_header_is_not_challenged(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    client = _app()

    resp = client.post(
        "/a2a/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "tasks/list", "params": {}},
        headers={"Authorization": "Bearer some.jwt.token"},
    )

    # Must not 402 a request carrying other auth — downstream decides.
    assert resp.status_code != 402


def test_misconfigured_x402_does_not_challenge(monkeypatch):
    # No X402_PAYMENT_RECIPIENT set -> nothing to pay to -> must not 402 a
    # bogus/empty challenge; behaves exactly like today (pass through).
    monkeypatch.delenv("X402_PAYMENT_RECIPIENT", raising=False)
    client = _app()

    resp = client.post(
        "/a2a/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "tasks/list", "params": {}},
    )
    assert resp.status_code != 402


def test_ungated_public_path_never_challenged(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    app.add_middleware(X402PaymentMiddleware, enabled=True)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/health")
    assert resp.status_code == 200


def test_api_task_prefix_not_in_gated_allowlist():
    """Documents the C7 decision: /api/task/* (the real REST mount) is
    deliberately absent from X402_GATED_PATH_PREFIXES — it's auth-gated by
    fallback_auth_middleware, so anonymous x402 is not offered there."""
    from modules.x402.middleware import X402PaymentMiddleware as MW
    mw = MW(app=FastAPI(), enabled=False)
    assert not mw._is_x402_gated("/api/task/sessions")
    assert not mw._is_x402_gated("/task/sessions")
