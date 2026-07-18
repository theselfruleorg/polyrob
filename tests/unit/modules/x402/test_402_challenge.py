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


def test_anonymous_request_to_real_gated_a2a_message_stream_gets_402_challenge(monkeypatch):
    """POST /a2a/message/stream is a real gated route (X402_GATED_ROUTES) but,
    per the reviewer note on task 16, had no dedicated coverage against the
    REAL mounted router — only a frozenset-membership check
    (test_api_task_prefix_not_in_gated_allowlist and friends cover other
    routes this way, but not this one). Exercise it exactly like the /a2a/rpc
    and /v1/chat/completions cases above: real router, real prefix, a
    genuinely anonymous caller must get a machine-readable 402 challenge, not
    fall through to the streaming endpoint."""
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("X402_PRICE_USD", "0.02")
    client = _app()

    resp = client.post(
        "/a2a/message/stream",
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
    )

    assert resp.status_code == 402
    body = resp.json()
    accepts = body["accepts"][0]
    assert accepts["payTo"] == "0x" + "1" * 40
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
    deliberately absent from the gated route set — it's auth-gated by
    fallback_auth_middleware, so anonymous x402 is not offered there."""
    from modules.x402.middleware import X402PaymentMiddleware as MW
    mw = MW(app=FastAPI(), enabled=False)
    assert not mw._is_x402_gated("/api/task/sessions")
    assert not mw._is_x402_gated("/task/sessions")


# ---------------------------------------------------------------------------
# G-18: read-vs-write gating. The old `path.startswith("/a2a/tasks")` prefix
# match 402-challenged every sub-route under it, including reads and
# continuations that api/a2a/endpoints.py + api/a2a/streaming.py never bill
# via verify_payment_for_request. Only exact (POST, path) creation routes may
# be challenged now.
# ---------------------------------------------------------------------------

def test_anonymous_get_task_by_id_not_challenged(monkeypatch):
    """GET /a2a/tasks/{id} is a pure read — the endpoint layer never bills it
    (api/a2a/endpoints.py::get_task has no verify_payment_for_request call).
    It must not be 402-challenged just because it shares the /a2a/tasks
    prefix with the (billed) create route."""
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    client = _app()

    resp = client.get("/a2a/tasks/some-task-id")

    assert resp.status_code != 402


def test_anonymous_list_tasks_not_challenged(monkeypatch):
    """GET /a2a/tasks (list) is also a free read."""
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    client = _app()

    resp = client.get("/a2a/tasks")

    assert resp.status_code != 402


def test_anonymous_send_to_existing_task_not_challenged(monkeypatch):
    """POST /a2a/tasks/{id}/send is a continuation of an already-paid task
    ("already paid" per api/a2a/endpoints.py::send_to_task) — never billed,
    so it must not be swept up by the old /a2a/tasks prefix match."""
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    client = _app()

    resp = client.post(
        "/a2a/tasks/some-task-id/send",
        json={"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
    )

    assert resp.status_code != 402


def test_anonymous_cancel_task_not_challenged(monkeypatch):
    """POST /a2a/tasks/{id}/cancel is never billed either."""
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    client = _app()

    resp = client.post("/a2a/tasks/some-task-id/cancel")

    assert resp.status_code != 402


def test_anonymous_resubscribe_not_challenged(monkeypatch):
    """POST /a2a/tasks/resubscribe (streaming reconnect) is also free — it
    used to match the same over-broad "/a2a/tasks" prefix."""
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    client = _app()

    resp = client.post("/a2a/tasks/resubscribe", params={"task_id": "some-task-id"})

    assert resp.status_code != 402


def test_anonymous_create_task_rest_endpoint_still_challenged(monkeypatch):
    """POST /a2a/tasks (create, exact) is the one route in this family that
    DOES bill (api/a2a/endpoints.py::create_task calls
    verify_payment_for_request) — the read/write split must not throw the
    baby out with the bathwater."""
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("X402_PRICE_USD", "0.02")
    client = _app()

    resp = client.post(
        "/a2a/tasks",
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
    )

    assert resp.status_code == 402
    body = resp.json()
    assert body["amount_usd"] == 0.02


# ---------------------------------------------------------------------------
# G-17: a request carrying X-PAYMENT on a gated path must never be silently
# dropped just because the fastapi-x402 SDK isn't importable/initialized —
# that used to fall through to call_next uncharged and 401 downstream with
# zero indication the payment was ever seen. `fastapi_x402` is not installed
# in this test environment, so `_facilitator_client` is naturally None here —
# exactly the "SDK unavailable" condition this test targets.
# ---------------------------------------------------------------------------

def test_payment_header_with_unavailable_facilitator_gets_honest_error(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    client = _app()
    assert client.app.user_middleware  # sanity: middleware attached

    resp = client.post(
        "/a2a/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "tasks/list", "params": {}},
        headers={"X-PAYMENT": "some-base64-payload"},
    )

    # Honest feedback, not a silent pass-through to a bare downstream 401.
    assert resp.status_code in (402, 503)
    assert resp.status_code != 401
    body = resp.json()
    assert "error" in body


def test_payment_header_no_other_auth_facilitator_down_is_exactly_503(monkeypatch):
    """Preserved G-17, pinned precisely: when X-PAYMENT is the caller's ONLY
    auth signal (no API key, no bearer token, no session cookie) and the
    facilitator is unavailable, the 503 must still fire — loudly, not a
    silent drop. This is the one case the 503 branch is FOR."""
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    client = _app()

    resp = client.post(
        "/a2a/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "tasks/list", "params": {}},
        headers={"X-PAYMENT": "some-base64-payload"},
    )

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "Payment settlement unavailable"


# ---------------------------------------------------------------------------
# Regression (fix pass 1, task 16): the G-17 503 branch above did NOT check
# for other auth signals before commit 1b08b665's follow-up fix. Since
# X402_ENABLED defaults to "false", `_facilitator_client` is None on ANY
# deployment that hasn't turned x402 on — the default/common case. An
# already-authenticated caller (valid X-API-KEY, or Authorization: Bearer)
# who ALSO sends a stray/defensive X-PAYMENT header used to get a hard 503,
# even though the identical request WITHOUT the X-PAYMENT header is served
# fine via JWT/credits. Before commit 1b08b665 this exact request fell
# through to call_next unconditionally. These use a minimal fake app (not
# the real a2a router) so the assertion is a direct, unambiguous "the
# middleware let it reach the downstream handler" signal, decoupled from the
# real router's own auth-dependency chain (which isn't wired into this
# minimal test app).
# ---------------------------------------------------------------------------

def _fake_gated_app():
    app = FastAPI()

    @app.post("/a2a/rpc")
    async def fake_rpc():
        return {"reached": "downstream"}

    app.add_middleware(X402PaymentMiddleware, enabled=True)
    return TestClient(app, raise_server_exceptions=False)


def test_stray_payment_header_with_api_key_not_503_reaches_downstream(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    client = _fake_gated_app()

    resp = client.post(
        "/a2a/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "tasks/list", "params": {}},
        headers={
            "X-PAYMENT": "some-base64-payload",
            "X-API-KEY": "rob_some_valid_looking_key",
        },
    )

    assert resp.status_code != 503
    assert resp.status_code == 200
    assert resp.json() == {"reached": "downstream"}


def test_stray_payment_header_with_bearer_token_not_503_reaches_downstream(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    client = _fake_gated_app()

    resp = client.post(
        "/a2a/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "tasks/list", "params": {}},
        headers={
            "X-PAYMENT": "some-base64-payload",
            "Authorization": "Bearer some.jwt.token",
        },
    )

    assert resp.status_code != 503
    assert resp.status_code == 200
    assert resp.json() == {"reached": "downstream"}
