"""Adversarial re-verification of the section-B API commit (694570f02).

Each test here pins a gap the original fix left open, found by reading the
shipped code rather than the commit message:

* the ``--workers`` refusal existed only in the CLI, so ``main.py`` — the
  systemd entry — started N autonomy runtimes silently;
* ``DependencyContainer.get_instance()`` RAISES on an uninitialized process, so
  every honest 503 in this tier ("wallet sign-in is not enabled…") was
  unreachable and the caller got a 500 traceback instead;
* the B45 strict-identity gate was applied to ONE of the three api-key routes;
* ``X-Service-Token`` was made canonical without being added to the CORS
  allow-list, so a browser client could not send it at all;
* ``refund_due`` was written to a status column no listing reads;
* ``supported_chains`` advertised testnet beside mainnet;
* ``/users/{id}/sessions`` answered "all sessions" with the ACTIVE ones only.
"""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# B13 — the multi-worker refusal belongs to the LAUNCH path, not the CLI
# ---------------------------------------------------------------------------

def test_multiworker_refusal_covers_every_launch_path(monkeypatch):
    from api.server_boot import multiworker_refusal

    monkeypatch.delenv("API_AUTONOMY_RUNTIME", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)

    assert multiworker_refusal(1) is None
    assert multiworker_refusal(None) is None
    refusal = multiworker_refusal(4)
    assert refusal and "API_AUTONOMY_RUNTIME=false" in refusal

    # The documented escape hatch really opens.
    monkeypatch.setenv("API_AUTONOMY_RUNTIME", "false")
    assert multiworker_refusal(4) is None


def test_multiworker_refusal_reads_the_env_worker_count(monkeypatch):
    from api.server_boot import multiworker_refusal

    monkeypatch.delenv("API_AUTONOMY_RUNTIME", raising=False)
    monkeypatch.setenv("UVICORN_WORKERS", "3")
    assert multiworker_refusal(None) is not None


def test_a_nonnumeric_worker_count_is_one_not_a_traceback(monkeypatch):
    """A typo in an env file must not take the server down with a ValueError."""
    from api.server_boot import resolve_workers

    monkeypatch.setenv("UVICORN_WORKERS", "two")
    assert resolve_workers(None) == 1
    monkeypatch.setenv("UVICORN_WORKERS", "")
    assert resolve_workers(None) == 1


def test_run_server_refuses_before_it_launches_uvicorn(monkeypatch):
    import api.server_boot as sb

    monkeypatch.setattr(sb, "_load_env", lambda: None)
    monkeypatch.delenv("API_AUTONOMY_RUNTIME", raising=False)
    with pytest.raises(SystemExit) as ei:
        sb.run_server(workers=2)
    assert ei.value.code == 1


def test_the_cli_uses_the_shared_seam_not_its_own_copy():
    import inspect

    from cli.commands import serve

    src = inspect.getsource(serve)
    assert "multiworker_refusal" in src
    assert "API_AUTONOMY_RUNTIME" not in src.split("multiworker_refusal")[-1], (
        "the CLI must not re-derive the rule it just imported")


# ---------------------------------------------------------------------------
# The container accessor RAISES — every honest 503 in this tier depended on it
# returning None
# ---------------------------------------------------------------------------

def _no_container(monkeypatch):
    from core.container import DependencyContainer

    monkeypatch.setattr(DependencyContainer, "_instance", None, raising=False)


def test_optional_container_is_none_when_none_was_built(monkeypatch):
    from api.dependencies import optional_container

    _no_container(monkeypatch)
    assert optional_container() is None


def test_require_service_is_503_not_500_without_a_container(monkeypatch):
    from api.dependencies import require_service

    _no_container(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        require_service("hyperliquid", missing="Hyperliquid service not available")
    assert ei.value.status_code == 503


def test_siwe_nonce_is_503_with_the_remedy_not_a_500(monkeypatch):
    """B19's message is written for exactly the instance that used to 500."""
    from api.auth_endpoints import router

    _no_container(monkeypatch)
    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/auth/nonce",
                       json={"wallet_address": "0x" + "1" * 40})
    assert resp.status_code == 503, resp.text
    assert "ENABLE_AUTH" in resp.json()["detail"]


def test_siwe_verify_is_503_with_the_remedy_not_a_500(monkeypatch):
    from api.auth_endpoints import router

    _no_container(monkeypatch)
    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/auth/verify", json={
        "wallet_address": "0x" + "1" * 40, "message": "m",
        "signature": "0x", "nonce": "n",
    })
    assert resp.status_code == 503, resp.text


# ---------------------------------------------------------------------------
# B45 — the strict identity gate reached ONE of the three api-key routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("get", "/api/auth/api-keys"),
    ("delete", "/api/auth/api-keys/rob_abc123"),
])
def test_listing_and_revoking_keys_refuse_an_anonymous_caller(
        method, path, monkeypatch):
    from api.auth_endpoints import router

    _no_container(monkeypatch)
    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    client = TestClient(app, raise_server_exceptions=False)
    assert getattr(client, method)(path).status_code == 401


def test_listing_keys_refuses_the_service_token_placeholder(monkeypatch):
    """`authenticated_api_user` is not a tenant — it must not own key rows."""
    from api.auth_endpoints import router

    _no_container(monkeypatch)
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.user_id = "authenticated_api_user"
        return await call_next(request)

    app.include_router(router, prefix="/api/auth")
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/auth/api-keys").status_code == 401


def test_me_reads_the_value_not_merely_the_attribute():
    """`hasattr` is True even when a middleware set user_id to None."""
    import asyncio

    from api.auth_endpoints import get_current_user

    class _State:
        user_id = None

    class _Req:
        state = _State()

    with pytest.raises(HTTPException) as ei:
        asyncio.run(get_current_user(_Req()))
    assert ei.value.status_code == 401


# ---------------------------------------------------------------------------
# B4 — the canonical header has to survive CORS preflight
# ---------------------------------------------------------------------------

def test_cors_allows_the_canonical_service_token_header():
    import inspect

    from api import app as app_module

    src = inspect.getsource(app_module.create_app)
    allow_block = src.split("allowed_headers = [")[1].split("]")[0]
    assert '"X-Service-Token"' in allow_block, (
        "the header the docs tell operators to send must pass preflight")
    assert '"X-PAYMENT"' in allow_block


# ---------------------------------------------------------------------------
# B14 — no import-time-bound cap constant
# ---------------------------------------------------------------------------

def test_the_body_cap_is_a_call_not_an_import_time_constant():
    import api.request_limits as rl

    assert not hasattr(rl, "MAX_BODY_BYTES"), (
        "a cap bound at import binds it before env layering")
    assert rl.max_body_bytes() > 0


# ---------------------------------------------------------------------------
# B22 — the advertised chain set must match the configured network
# ---------------------------------------------------------------------------

def test_a_mainnet_instance_does_not_advertise_testnet(monkeypatch):
    import api.x402_advertisement as adv

    monkeypatch.setattr(adv, "_facilitator_assets", lambda: [
        type("A", (), {"symbol": "USDC", "chain": "base"})(),
        type("A", (), {"symbol": "USDC", "chain": "base-sepolia"})(),
    ])
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    assert adv.supported_chains() == ["base"]

    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base-sepolia")
    assert adv.supported_chains() == ["base-sepolia"]


def test_an_unknown_network_lists_every_rail_chain_not_none(monkeypatch):
    import api.x402_advertisement as adv

    monkeypatch.setattr(adv, "_facilitator_assets", lambda: [
        type("A", (), {"symbol": "USDC", "chain": "base"})(),
    ])
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "some-future-chain")
    assert adv.supported_chains() == ["base"], "never an empty list"


# ---------------------------------------------------------------------------
# refund_due is written where nothing reads — it must leave a durable trace
# ---------------------------------------------------------------------------

def test_refund_due_emits_a_durable_event(monkeypatch):
    import asyncio

    import modules.x402.x402_integration as xi

    class _DB:
        async def execute(self, *a, **k):
            return None

    class _Container:
        def get_service(self, name):
            return _DB()

    from core.container import DependencyContainer
    monkeypatch.setattr(DependencyContainer, "get_instance",
                        classmethod(lambda cls, config=None: _Container()))

    seen = {}
    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "_emit", lambda kind, **kw: seen.update(
        {"kind": kind, **kw}))

    assert asyncio.run(xi.mark_payment_refund_due("x402_r")) is True
    assert seen["kind"] == xi.PAYMENT_REFUND_DUE_EVENT
    assert seen["attrs"]["payment_id"] == "x402_r"


# ---------------------------------------------------------------------------
# B7 — "all sessions" must not silently mean "the running ones"
# ---------------------------------------------------------------------------

def test_user_sessions_lists_a_completed_session(monkeypatch):
    import asyncio

    from api.task_http_api import get_user_sessions

    rows = [
        {"id": "s-live", "user_id": "u1", "status": "running"},
        {"id": "s-done", "user_id": "u1", "status": "completed"},
        {"id": "s-other", "user_id": "u2", "status": "running"},
    ]

    class _SM:
        def get_active_sessions(self, user_id=None):
            return ["s-live"]

        def get_all_sessions(self):
            return rows

    class _Agent:
        session_manager = _SM()
        user_sessions = {}

        async def get_session_by_id(self, sid):
            return next((r for r in rows if r["id"] == sid), None)

    class _State:
        user_id = "u1"
        role = "user"

    class _Req:
        state = _State()

    out = asyncio.run(get_user_sessions("u1", _Req(), _Agent()))
    ids = [s["id"] for s in out["sessions"]]
    assert ids == ["s-live", "s-done"], ids
    assert "s-other" not in ids


# ---------------------------------------------------------------------------
# Vocabulary + seam pins
# ---------------------------------------------------------------------------

def test_session_status_sets_name_only_real_statuses():
    from agents.task.agent.session import SessionStatus
    from api.task_http_api import (RESUMABLE_SESSION_STATUSES,
                                   TERMINAL_SESSION_STATUSES)

    real = {s.value for s in SessionStatus}
    assert TERMINAL_SESSION_STATUSES <= real
    assert RESUMABLE_SESSION_STATUSES <= real


def test_the_shipped_chat_seam_accepts_a_per_request_temperature():
    """If it did not, /v1 would 400 every `temperature` request (B18)."""
    import inspect

    from agents.task.task_agent_chat import TaskAgentChatMixin

    assert "temperature" in inspect.signature(
        TaskAgentChatMixin.chat_once).parameters


# ---------------------------------------------------------------------------
# B1/B18 — proved on the REAL app, both gates mounted
# ---------------------------------------------------------------------------

DOCUMENTED_PUBLIC = [
    "/health",
    "/.well-known/agent.json",
    "/a2a/agent-card",
    "/api/x402/pricing",
    "/api/pricing/models",
    "/eip8004/registration.json",
]


@pytest.fixture()
def _real_app(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_SECRET", "s" * 40)
    monkeypatch.setenv("JWT_SECRET_KEY", "j" * 40)
    monkeypatch.setenv("API_AUTH_TOKEN", "operator-token")
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    monkeypatch.setenv("OPENAI_COMPAT_API_ENABLED", "true")
    from api.app import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.mark.parametrize("path", DOCUMENTED_PUBLIC)
def test_every_documented_public_path_passes_both_gates(_real_app, path):
    assert _real_app.get(path).status_code != 401, path


@pytest.mark.parametrize("path", [
    "/api/task/sessions", "/api/admin/users", "/api/task/metrics", "/a2a/rpc",
])
def test_a_protected_path_still_401s(_real_app, path):
    assert _real_app.get(path).status_code == 401, path


def test_the_api_key_middleware_sits_outside_the_fallback_and_never_refuses(
        _real_app):
    names = [m.cls.__name__ for m in _real_app.app.user_middleware]
    assert "APIKeyAuthMiddleware" in names
    # Starlette's list is outermost-first; the fallback is the innermost
    # BaseHTTPMiddleware, so the key validator must come BEFORE it.
    assert names.index("APIKeyAuthMiddleware") < names.index("BaseHTTPMiddleware")
    # And it refuses nothing: an unknown key on a public path is still served.
    resp = _real_app.get("/health", headers={"X-API-KEY": "rob_" + "z" * 40})
    assert resp.status_code in (200, 503)  # never 401


def test_a_v1_error_is_openai_shaped_and_a_task_error_is_not(_real_app):
    v1 = _real_app.post("/v1/chat/completions", json={"messages": []})
    assert isinstance(v1.json().get("error"), dict), v1.text
    assert "type" in v1.json()["error"]

    native = _real_app.get("/api/task/sessions")
    assert isinstance(native.json().get("error"), (str, dict))
    if isinstance(native.json()["error"], dict):
        assert "type" not in native.json()["error"]


def test_the_v1_envelope_covers_a_middleware_refusal_not_only_a_route(_real_app):
    """`AuthenticationMiddleware` never passes through the app's handlers."""
    body = _real_app.post("/v1/chat/completions", json={"messages": []}).json()
    assert body["error"]["type"] == "authentication_error"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_a_402_keeps_the_x402_challenge_shape():
    """A payment challenge belongs to the payment protocol, not to OpenAI."""
    from starlette.responses import JSONResponse

    from api.openai_compat.errors import OpenAICompatErrorMiddleware

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def _paid():
        return JSONResponse(status_code=402, content={"accepts": [{"asset": "usdc"}]})

    app.add_middleware(OpenAICompatErrorMiddleware)
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/v1/chat/completions", json={})
    assert resp.status_code == 402
    assert resp.json() == {"accepts": [{"asset": "usdc"}]}


def test_a_successful_v1_stream_is_untouched():
    from starlette.responses import StreamingResponse

    from api.openai_compat.errors import OpenAICompatErrorMiddleware

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def _stream():
        async def gen():
            yield b"data: one\n\n"
            yield b"data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    app.add_middleware(OpenAICompatErrorMiddleware)
    resp = TestClient(app).post("/v1/chat/completions", json={})
    assert resp.status_code == 200
    assert resp.text.endswith("data: [DONE]\n\n")


def test_a_non_v1_path_is_never_reshaped():
    from starlette.responses import JSONResponse

    from api.openai_compat.errors import OpenAICompatErrorMiddleware

    app = FastAPI()

    @app.get("/api/task/sessions")
    async def _native():
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    app.add_middleware(OpenAICompatErrorMiddleware)
    resp = TestClient(app, raise_server_exceptions=False).get("/api/task/sessions")
    assert resp.json() == {"error": "Unauthorized"}


def test_health_never_500s_on_an_unreadable_metric(monkeypatch):
    """A liveness probe may say degraded; it may never invent a server error."""
    import asyncio

    import api.app as app_module

    class _Agent:
        max_sessions_in_memory = object()  # not a number

        def active_session_count(self):
            return object()

    class _Container:
        def get_agent(self, name):
            return _Agent()

    monkeypatch.setitem(app_module.app_state, "bot", object())
    monkeypatch.setitem(app_module.app_state, "container", _Container())

    app = app_module.create_app()
    resp = TestClient(app, raise_server_exceptions=False).get("/health")
    assert resp.status_code in (200, 503), resp.text
    body = resp.json()
    assert body["metrics"]["active_sessions"] is None


def test_an_owner_login_is_reported_as_admin():
    """`role == 'admin'` misses "owner" — the H1 shape, one site still had it."""
    import inspect

    from api import auth_endpoints

    src = inspect.getsource(auth_endpoints.verify_signature)
    assert "role == 'admin'" not in src
    assert "is_admin_role(role)" in src
    from api.auth_constants import is_admin_role
    assert is_admin_role("owner") and is_admin_role("admin")
    assert not is_admin_role("service") and not is_admin_role("user")


def test_a_stray_payment_header_on_a_free_route_is_never_settled(monkeypatch):
    """B3 proved at DISPATCH, not only on the predicate.

    The settle branch used to fire on ANY path carrying `X-PAYMENT`, so a free
    read with a stray header took the payer's money.
    """
    from starlette.responses import JSONResponse

    from modules.x402.middleware import X402PaymentMiddleware

    settled = []

    async def _explode(self, request, call_next, header):
        settled.append(request.url.path)
        return JSONResponse({"charged": True})

    # monkeypatch so the stub is UNDONE — this class is shared by every other
    # test in the session.
    monkeypatch.setattr(X402PaymentMiddleware, "_handle_x402_payment", _explode)

    app = FastAPI()

    @app.post("/api/task/sessions")
    async def _free():
        return JSONResponse({"ok": True})

    app.add_middleware(X402PaymentMiddleware, enabled=True)
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/api/task/sessions", json={}, headers={"X-PAYMENT": "base64-payload"})
    assert settled == [], "a free route must never be settled"
    assert resp.status_code != 402
