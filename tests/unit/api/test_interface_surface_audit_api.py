"""Regression tests for the 2026-10-03 interface + surface audit, section 9
(HTTP API, A2A, /v1) — ids API1..API19.

Each test pins the defect the audit named.
"""
from core.security.session_tokens import SESSION_AUDIENCE
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# API1 — an API-key caller must not mint or revoke API keys
# ---------------------------------------------------------------------------

class _FakeKeyManager:
    def __init__(self):
        self.minted = []
        self.revoked = []

    async def generate_api_key(self, user_id, name, expires_days=90, scopes=('read', 'write')):
        self.minted.append(user_id)
        return {"api_key": "rob_" + "x" * 40, "name": name,
                "prefix": "rob_xxxxxx", "expires_at": "2030-01-01", "created_at": "t",
                "warning": "w", "scopes": list(scopes)}

    async def revoke_key(self, user_id, prefix):
        self.revoked.append((user_id, prefix))
        return True

    async def list_user_keys(self, user_id):
        return []


class _FakeContainer:
    def __init__(self, mgr):
        self._mgr = mgr

    def get_service(self, name):
        return self._mgr if name == "api_key_manager" else None


def _key_app(monkeypatch, auth_method):
    from api import dependencies
    from api.auth_endpoints import router

    mgr = _FakeKeyManager()
    monkeypatch.setattr(dependencies, "optional_container",
                        lambda: _FakeContainer(mgr))
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.user_id = "0xOwner"
        request.state.authenticated = True
        request.state.auth_method = auth_method
        return await call_next(request)

    app.include_router(router, prefix="/api/auth")
    return TestClient(app, raise_server_exceptions=False), mgr


def test_api1_an_api_key_cannot_mint_a_key(monkeypatch):
    client, mgr = _key_app(monkeypatch, "api_key")
    resp = client.post("/api/auth/api-keys", json={"name": "n"})
    assert resp.status_code == 403, resp.text
    assert mgr.minted == []


def test_api1_an_api_key_cannot_revoke_a_key(monkeypatch):
    client, mgr = _key_app(monkeypatch, "api_key")
    resp = client.delete("/api/auth/api-keys/rob_abc123")
    assert resp.status_code == 403, resp.text
    assert mgr.revoked == []


def test_api1_a_wallet_session_still_manages_keys(monkeypatch):
    client, mgr = _key_app(monkeypatch, None)
    assert client.post("/api/auth/api-keys",
                       json={"name": "n"}).status_code == 200
    assert client.delete("/api/auth/api-keys/rob_abc123").status_code == 200
    assert mgr.minted == ["0xOwner"]


def test_api1_authentication_middleware_marks_an_api_key_identity():
    """The outer middleware authenticates keys too; it must set auth_method."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api.middleware import AuthenticationMiddleware

    mw = AuthenticationMiddleware(app=None, secret_key="s" * 40)
    mw._validate_auth = AsyncMock(return_value={
        "user_id": "0xOwner", "authenticated": True, "auth_method": "api_key"})
    mw._check_permissions = AsyncMock(return_value=True)
    state = SimpleNamespace()
    req = SimpleNamespace(
        url=SimpleNamespace(path="/api/task/sessions"),
        headers={"X-API-Key": "rob_" + "k" * 40},
        state=state,
    )

    async def _next(_req):
        return "ok"

    assert asyncio.run(mw.dispatch(req, _next)) == "ok"
    assert state.auth_method == "api_key"


def test_api1_never_expiring_keys_are_refused_before_minting(monkeypatch):
    client, mgr = _key_app(monkeypatch, None)
    resp = client.post("/api/auth/api-keys", json={"name": "n", "expires_days": None})
    assert resp.status_code == 422, resp.text
    assert not mgr.minted


@pytest.fixture()
def real_app(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_SECRET", "s" * 40)
    monkeypatch.setenv("JWT_SECRET_KEY", "j" * 40)
    monkeypatch.setenv("API_AUTH_TOKEN", "operator-token")
    monkeypatch.setenv("OPENAI_COMPAT_API_ENABLED", "true")
    from api.app import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def test_api1_end_to_end_an_x_api_key_cannot_mint(monkeypatch, real_app):
    from api import api_key_auth, dependencies

    async def _valid(key):
        return {"user_id": "0xOwner", "tier": "free", "role": "user",
                "auth_method": "api_key", "permissions": ['read', 'write'],
                "api_key_expires_at": 4102444800}

    monkeypatch.setattr(api_key_auth, "validate_api_key", _valid)
    mgr = _FakeKeyManager()
    monkeypatch.setattr(dependencies, "optional_container",
                        lambda: _FakeContainer(mgr))
    resp = real_app.post("/api/auth/api-keys", json={"name": "n"},
                         headers={"X-API-KEY": "rob_" + "k" * 40})
    assert resp.status_code == 403, resp.text
    assert mgr.minted == []


# ---------------------------------------------------------------------------
# API2 — the AuthenticationMiddleware key cache honoured no TTL / revocation
# ---------------------------------------------------------------------------

def _patched_validator(monkeypatch, answers):
    from api import api_key_auth

    async def _validate(key):
        return dict(answers[0], permissions=['read', 'write'], api_key_expires_at=4102444800) if answers[0] else None

    monkeypatch.setattr(api_key_auth, "validate_api_key", _validate)


def test_api2_a_revoked_key_stops_working_at_once(monkeypatch):
    import asyncio

    from api import api_key_auth
    from api.middleware import AuthenticationMiddleware

    answers = [{"user_id": "0xOwner", "auth_method": "api_key"}]
    _patched_validator(monkeypatch, answers)
    mw = AuthenticationMiddleware(app=None, secret_key="s" * 40)
    key = "rob_" + "k" * 40
    assert asyncio.run(mw._validate_api_key(key))["user_id"] == "0xOwner"
    answers[0] = None  # the row is now revoked
    api_key_auth.note_key_revoked()
    assert asyncio.run(mw._validate_api_key(key)) is None


def test_api2_the_key_cache_expires(monkeypatch):
    import asyncio

    from api import api_key_auth
    from api.middleware import AuthenticationMiddleware

    answers = [{"user_id": "0xOwner", "auth_method": "api_key"}]
    _patched_validator(monkeypatch, answers)
    clock = [1000.0]
    monkeypatch.setattr(api_key_auth, "_now", lambda: clock[0])
    mw = AuthenticationMiddleware(app=None, secret_key="s" * 40)
    key = "rob_" + "k" * 40
    assert asyncio.run(mw._validate_api_key(key)) is not None
    answers[0] = None
    clock[0] += api_key_auth._CACHE_TTL_SEC + 1
    assert asyncio.run(mw._validate_api_key(key)) is None


# ---------------------------------------------------------------------------
# API3 — the wallet JWT's `sub` is the wallet; the account is `user_id`
# ---------------------------------------------------------------------------

def _wallet_jwt(secret, **extra):
    import time as _t

    import jwt
    payload = {"sub": "0xWALLET", "user_id": "user-42", "tier": "free",
               "role": "user", "jti": "j1", "exp": int(_t.time()) + 600}
    payload.update(extra)
    return jwt.encode({"aud": SESSION_AUDIENCE, **payload}, secret, algorithm="HS256")


def test_api3_authentication_middleware_keeps_the_account_id(monkeypatch):
    import asyncio

    from api.middleware import AuthenticationMiddleware

    monkeypatch.setenv("JWT_SECRET_KEY", "j" * 40)
    mw = AuthenticationMiddleware(app=None, secret_key="s" * 40)
    info = asyncio.run(mw._validate_auth("Bearer " + _wallet_jwt("j" * 40), ""))
    assert info["user_id"] == "user-42"


def test_api3_a_token_without_user_id_still_falls_back_to_sub(monkeypatch):
    import asyncio

    from api.middleware import AuthenticationMiddleware

    monkeypatch.setenv("JWT_SECRET_KEY", "j" * 40)
    mw = AuthenticationMiddleware(app=None, secret_key="s" * 40)
    tok = _wallet_jwt("j" * 40, user_id=None)
    info = asyncio.run(mw._validate_auth("Bearer " + tok, ""))
    assert info["user_id"] == "0xWALLET"


def test_api3_the_real_stack_resolves_the_account_id(monkeypatch, real_app):
    from fastapi import Request
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    monkeypatch.setattr("api.dependencies.require_service", lambda *a, **kw:
                        SimpleNamespace(fetch_one=AsyncMock(return_value=None)))

    seen = {}

    @real_app.app.get("/api/_probe_identity")
    async def _probe(request: Request):
        seen["user_id"] = request.state.user_id
        return {"ok": True}

    resp = real_app.get("/api/_probe_identity", headers={
        "Authorization": "Bearer " + _wallet_jwt("j" * 40)})
    assert resp.status_code == 200, resp.text
    assert seen["user_id"] == "user-42"


# ---------------------------------------------------------------------------
# API4 — a settled x402 payer is authenticated; API_SECRET must not 401 it
# ---------------------------------------------------------------------------

def _dispatch(mw, path, headers, state):
    import asyncio
    from types import SimpleNamespace

    req = SimpleNamespace(url=SimpleNamespace(path=path), headers=headers,
                          state=state, method="POST", cookies={})

    async def _next(_req):
        return "served"

    return asyncio.run(mw.dispatch(req, _next))


def test_api4_a_paid_x402_caller_passes_authentication_middleware():
    from types import SimpleNamespace

    from api.middleware import AuthenticationMiddleware

    mw = AuthenticationMiddleware(app=None, secret_key="s" * 40)
    state = SimpleNamespace(user_id="usr_abc", authenticated=True,
                            payment_method="x402", tier="x402", role="user")
    assert _dispatch(mw, "/a2a/rpc", {}, state) == "served"
    assert state.user_id == "usr_abc"


def test_api4_an_anonymous_caller_still_401s():
    from types import SimpleNamespace

    from api.middleware import AuthenticationMiddleware

    mw = AuthenticationMiddleware(app=None, secret_key="s" * 40)
    resp = _dispatch(mw, "/a2a/rpc", {}, SimpleNamespace())
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# API5 — the operator X-Service-Token passes AuthenticationMiddleware
# ---------------------------------------------------------------------------

def test_api5_the_service_token_header_authenticates(monkeypatch):
    from types import SimpleNamespace

    from api.middleware import AuthenticationMiddleware

    monkeypatch.setenv("API_AUTH_TOKEN", "operator-token")
    mw = AuthenticationMiddleware(app=None, secret_key="s" * 40)
    state = SimpleNamespace()
    out = _dispatch(mw, "/api/task/sessions",
                    {"X-Service-Token": "operator-token"}, state)
    assert out == "served"
    assert state.role == "service" and state.is_admin is False
    assert state.user_id == "authenticated_api_user"


def test_api5_a_wrong_service_token_still_401s(monkeypatch):
    from types import SimpleNamespace

    from api.middleware import AuthenticationMiddleware

    monkeypatch.setenv("API_AUTH_TOKEN", "operator-token")
    mw = AuthenticationMiddleware(app=None, secret_key="s" * 40)
    out = _dispatch(mw, "/api/task/sessions",
                    {"X-Service-Token": "nope"}, SimpleNamespace())
    assert out.status_code == 401


def test_api5_the_service_token_is_not_admin(monkeypatch):
    from types import SimpleNamespace

    from api.middleware import AuthenticationMiddleware

    monkeypatch.setenv("API_AUTH_TOKEN", "operator-token")
    mw = AuthenticationMiddleware(app=None, secret_key="s" * 40)
    out = _dispatch(mw, "/api/admin/users",
                    {"X-Service-Token": "operator-token"}, SimpleNamespace())
    assert out.status_code == 403


def test_api5_end_to_end_service_token_passes_both_gates(real_app):
    resp = real_app.get("/api/task/sessions",
                        headers={"X-Service-Token": "operator-token"})
    assert resp.status_code != 401, resp.text


# ---------------------------------------------------------------------------
# API6 — `Authorization: Bearer rob_…` reaches the API-key validators
# ---------------------------------------------------------------------------

def test_api6_jwt_middleware_passes_a_non_jwt_bearer_through():
    from types import SimpleNamespace

    from api.jwt_middleware import JWTAuthMiddleware

    mw = JWTAuthMiddleware(app=None, jwt_secret="j" * 40)
    out = _dispatch(mw, "/api/task/sessions",
                    {"Authorization": "Bearer rob_" + "k" * 40},
                    SimpleNamespace())
    assert out == "served"


def test_api6_a_forged_jwt_still_401s():
    from types import SimpleNamespace

    from api.jwt_middleware import JWTAuthMiddleware

    mw = JWTAuthMiddleware(app=None, jwt_secret="j" * 40)
    out = _dispatch(mw, "/api/task/sessions",
                    {"Authorization": "Bearer " + _wallet_jwt("x" * 40)},
                    SimpleNamespace())
    assert out.status_code == 401


def test_api6_end_to_end_bearer_api_key(monkeypatch, real_app):
    from api import api_key_auth

    async def _valid(key):
        return {"user_id": "0xOwner", "tier": "free", "role": "user",
                "auth_method": "api_key", "permissions": ['read', 'write'],
                "api_key_expires_at": 4102444800}

    monkeypatch.setattr(api_key_auth, "validate_api_key", _valid)
    resp = real_app.get("/api/task/sessions",
                        headers={"Authorization": "Bearer rob_" + "k" * 40})
    assert resp.status_code != 401, resp.text


# ---------------------------------------------------------------------------
# API7 — a body-less request takes no upload slot; slots are bounded per client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api7_health_is_served_while_every_slot_is_held():
    import asyncio

    from api.request_limits import RequestBodyLimitMiddleware

    hold = asyncio.Event()
    served = []

    async def app(scope, receive, send):
        if scope["path"] == "/health":
            served.append("health")
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
            return
        await hold.wait()

    async def slow_receive():
        await asyncio.Event().wait()

    sent = []

    async def send(message):
        sent.append(message)

    mw = RequestBodyLimitMiddleware(app, concurrency=2, per_client=2, timeout=5)
    uploads = [asyncio.create_task(mw(
        {"type": "http", "method": "POST", "path": "/up",
         "headers": [(b"transfer-encoding", b"chunked")],
         "client": (f"10.0.0.{i}", 1)}, slow_receive, send)) for i in range(2)]
    for _ in range(500):
        if mw.slots.locked():
            break
        await asyncio.sleep(0.01)
    assert mw.slots.locked()
    await mw({"type": "http", "method": "GET", "path": "/health",
              "headers": [], "client": ("10.0.0.9", 1)}, slow_receive, send)
    assert served == ["health"]
    for t in uploads:
        t.cancel()
    await asyncio.gather(*uploads, return_exceptions=True)
    assert not mw.slots.locked() and mw.client_slots == {}


@pytest.mark.asyncio
async def test_api7_one_client_cannot_take_every_slot():
    import asyncio

    from api.request_limits import RequestBodyLimitMiddleware

    async def app(scope, receive, send):
        pytest.fail("an incomplete upload reached the app")

    async def slow_receive():
        await asyncio.Event().wait()

    sent = []

    async def send(message):
        sent.append(message)

    mw = RequestBodyLimitMiddleware(app, concurrency=8, per_client=2, timeout=5)
    scope = {"type": "http", "method": "POST", "path": "/up",
             "headers": [(b"content-length", b"10")], "client": ("1.2.3.4", 1)}
    held = [asyncio.create_task(mw(dict(scope), slow_receive, send))
            for _ in range(2)]
    for _ in range(500):
        if mw.client_slots.get("1.2.3.4") == 2:
            break
        await asyncio.sleep(0.01)
    await mw(dict(scope), slow_receive, send)
    assert sent[0]["status"] == 503
    # Another client still gets a slot.
    other = asyncio.create_task(mw(dict(scope, client=("5.6.7.8", 1)),
                                   slow_receive, send))
    for _ in range(500):
        if mw.client_slots.get("5.6.7.8") == 1:
            break
        await asyncio.sleep(0.01)
    assert mw.client_slots.get("5.6.7.8") == 1
    for t in held + [other]:
        t.cancel()
    await asyncio.gather(*held, other, return_exceptions=True)
    assert mw.client_slots == {}


# ---------------------------------------------------------------------------
# API8 — the A2A handler is per request; push config must live in the mirror
# ---------------------------------------------------------------------------

class _PushAgent:
    def __init__(self):
        self.info = {"user_id": "u1", "status": "running", "config": {},
                     "created_at": "", "updated_at": ""}
        agent = self

        class _SM:
            def update_session_metadata(self, sid, update):
                agent.info.update(update)

        self.session_manager = _SM()

    async def get_session_by_id(self, task_id):
        return dict(self.info)


class _PushContainer:
    def __init__(self, agent):
        self._agent = agent

    def get_agent(self, name):
        return self._agent

    def get_service(self, name):
        return None


def test_api8_push_config_survives_a_fresh_handler(monkeypatch):
    import asyncio

    import api.a2a.task_handler as th
    from api.a2a.models import PushNotificationConfig

    monkeypatch.setattr(th, "validate_push_url", lambda url: url)
    agent = _PushAgent()

    def fresh():  # get_task_handler builds one per request
        return th.A2ATaskHandler(_PushContainer(agent))

    cfg = PushNotificationConfig(url="https://cb.example/hook", token="tok")
    assert asyncio.run(fresh().set_push_notification_config("t1", cfg, "u1"))
    got = asyncio.run(fresh().get_push_notification_config("t1", "u1"))
    assert got is not None and got.url == "https://cb.example/hook"
    assert asyncio.run(fresh().delete_push_notification_config("t1", "u1"))
    # A deleted webhook must not fire from the mirror any more.
    assert fresh()._get_push_config("t1", dict(agent.info)) is None
    assert asyncio.run(fresh().get_push_notification_config("t1", "u1")) is None


def test_api8_a_nested_metadata_mirror_is_cleared_too(monkeypatch):
    import asyncio

    import api.a2a.task_handler as th

    monkeypatch.setattr(th, "validate_push_url", lambda url: url)
    agent = _PushAgent()
    agent.info["metadata"] = {"a2a_push_url": "https://cb.example/old",
                              "keep": 1}
    h = th.A2ATaskHandler(_PushContainer(agent))
    assert asyncio.run(h.delete_push_notification_config("t1", "u1"))
    assert agent.info["metadata"] == {"keep": 1}
    assert h._get_push_config("t1", dict(agent.info)) is None


# ---------------------------------------------------------------------------
# API9 — a field_validator error is a 422, not a 500
# ---------------------------------------------------------------------------

def test_api9_a_field_validator_error_is_422(real_app):
    from pydantic import BaseModel, field_validator

    class _Body(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def _check(cls, v):
            raise ValueError("bad name")

    @real_app.app.post("/api/_probe_validate")
    async def _probe(body: _Body):
        return {"ok": True}

    resp = real_app.post("/api/_probe_validate", json={"name": "x"},
                         headers={"X-Service-Token": "operator-token"})
    assert resp.status_code == 422, resp.text
    assert "bad name" in resp.text


def test_api9_a_v1_field_validator_error_is_400(real_app):
    from pydantic import BaseModel, field_validator

    class _Body(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def _check(cls, v):
            raise ValueError("bad name")

    @real_app.app.post("/v1/_probe_validate")
    async def _probe(body: _Body):
        return {"ok": True}

    resp = real_app.post("/v1/_probe_validate", json={"name": "x"},
                         headers={"X-Service-Token": "operator-token"})
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# API10 — x402 settles only for a NEW message/send; free reads stay free
# ---------------------------------------------------------------------------

def _x402_app(monkeypatch):
    from fastapi import Request

    from modules.x402 import middleware as xm

    settled = []

    def _init(self):
        self._facilitator_client = object()

    async def _handle(self, request, call_next, header):
        settled.append(request.url.path)
        return await call_next(request)

    monkeypatch.setattr(xm.X402PaymentMiddleware, "_init_facilitator", _init)
    monkeypatch.setattr(xm.X402PaymentMiddleware, "_handle_x402_payment", _handle)
    app = FastAPI()
    app.add_middleware(xm.X402PaymentMiddleware, enabled=True)

    @app.post("/a2a/rpc")
    async def _rpc(request: Request):
        return {"echo": await request.json()}

    @app.post("/a2a/message/stream")
    async def _stream(request: Request):
        return {"echo": await request.json()}

    return TestClient(app, raise_server_exceptions=False), settled


def _rpc(method, params=None):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


@pytest.mark.parametrize("body", [
    _rpc("tasks/get", {"id": "t1"}),
    _rpc("tasks/list"),
    _rpc("tasks/cancel", {"id": "t1"}),
    _rpc("message/send", {"message": {"role": "user", "taskId": "t1",
                                      "parts": [{"kind": "text", "text": "hi"}]}}),
])
def test_api10_a_free_rpc_method_is_never_settled(monkeypatch, body):
    client, settled = _x402_app(monkeypatch)
    resp = client.post("/a2a/rpc", json=body, headers={"X-PAYMENT": "e30="})
    assert settled == []
    assert resp.status_code == 200 and resp.json()["echo"] == body


def test_api10_a_new_message_send_is_settled(monkeypatch):
    client, settled = _x402_app(monkeypatch)
    body = _rpc("message/send", {"message": {
        "role": "user", "parts": [{"kind": "text", "text": "hi"}]}})
    resp = client.post("/a2a/rpc", json=body, headers={"X-PAYMENT": "e30="})
    assert settled == ["/a2a/rpc"]
    assert resp.json()["echo"] == body


def test_api10_a_stream_continuation_is_not_settled(monkeypatch):
    client, settled = _x402_app(monkeypatch)
    body = {"message": {"role": "user", "taskId": "t1",
                        "parts": [{"kind": "text", "text": "hi"}]}}
    client.post("/a2a/message/stream", json=body, headers={"X-PAYMENT": "e30="})
    assert settled == []
    client.post("/a2a/message/stream", json={"message": {
        "role": "user", "parts": []}}, headers={"X-PAYMENT": "e30="})
    assert settled == ["/a2a/message/stream"]


def test_api10_a_free_rpc_read_gets_no_402_challenge(monkeypatch):
    from modules.x402 import middleware as xm

    monkeypatch.setattr(xm, "get_x402_config", lambda: {"pay_to": "0xabc"})
    client, _ = _x402_app(monkeypatch)
    resp = client.post("/a2a/rpc", json=_rpc("tasks/get", {"id": "t1"}))
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# API11 — a failure AFTER settlement records refund-due and leaks nothing
# ---------------------------------------------------------------------------

def _settling_app(monkeypatch, *, fail_at):
    import base64
    import json
    from types import SimpleNamespace

    from fastapi import Request

    from modules.x402 import middleware as xm

    calls = {"recorded": [], "refund_due": []}

    class _Facilitator:
        async def verify_and_settle_payment(self, payment_header,
                                            payment_requirements, before_settle=None):
            return (SimpleNamespace(isValid=True, error=None),
                    SimpleNamespace(success=True, errorReason=None,
                                    transaction="0x" + "b" * 64))

    def _init(self):
        self._facilitator_client = _Facilitator()

    async def _profile(addr, uid):
        if fail_at == "profile":
            raise RuntimeError("boom at /secret/path/bot.db")
        return True

    async def _record(**kw):
        calls["recorded"].append(kw["payment_id"])
        if fail_at == "record":
            raise RuntimeError("boom at /secret/path/bot.db")
        return True

    async def _refund(pid):
        calls["refund_due"].append(pid)
        return True

    monkeypatch.setattr(xm.X402PaymentMiddleware, "_init_facilitator", _init)
    monkeypatch.setattr(xm, "get_x402_config", lambda: {
        "network": "base-sepolia", "pay_to": "0x" + "a" * 40, "enabled": True})
    monkeypatch.setattr(xm, "get_x402_price_usd", lambda: 0.01)
    monkeypatch.setattr(xm, "ensure_user_profile_for_payer", _profile)
    async def _resolve(addr):
        return "usr_" + "0" * 16
    monkeypatch.setattr(xm, "resolve_payer_user_id", _resolve)
    monkeypatch.setattr(xm, "record_x402_payment", _record)
    monkeypatch.setattr(xm, "mark_payment_refund_due", _refund)
    monkeypatch.setattr(xm, "resolve_owner_user_id", lambda: "owner")
    from api.auth_state import set_auth_state
    xm.install_auth_state_writer(set_auth_state)

    app = FastAPI()
    app.add_middleware(xm.X402PaymentMiddleware, enabled=True)

    @app.post("/v1/chat/completions")
    async def _paid(request: Request):
        if fail_at == "route":
            raise RuntimeError("boom at /secret/path/bot.db")
        return {"ok": True}

    header = base64.b64encode(json.dumps({"payload": {"authorization": {
        "from": "0x" + "c" * 40}}}).encode()).decode()
    return TestClient(app, raise_server_exceptions=False), header, calls


@pytest.mark.parametrize("fail_at", ["profile", "record", "route"])
def test_api11_post_settlement_failure_is_refund_due(monkeypatch, fail_at):
    pytest.importorskip("fastapi_x402")
    client, header, calls = _settling_app(monkeypatch, fail_at=fail_at)
    resp = client.post("/v1/chat/completions", json={},
                       headers={"X-PAYMENT": header})
    assert resp.status_code == 500
    assert "/secret/path" not in resp.text and "boom" not in resp.text
    assert "ref" in resp.text.lower()
    assert len(calls["refund_due"]) == 1
    # A refund-due needs a row to flag: the payment is recorded first.
    assert calls["recorded"] and calls["recorded"][0] == calls["refund_due"][0]


# ---------------------------------------------------------------------------
# API12 — /api/chat/message carries the same payment gate as /v1 and A2A
# ---------------------------------------------------------------------------

def _chat_endpoint():
    from api.app import create_app

    for r in create_app().routes:
        if getattr(r, "path", None) == "/api/chat/message":
            return r.endpoint
    raise AssertionError("/api/chat/message endpoint not registered")


def test_api12_an_unpaid_chat_message_is_402_and_never_runs(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from api.app import app_state
    from api.models import MessageRequest

    endpoint = _chat_endpoint()
    monkeypatch.setitem(app_state, "container", MagicMock())
    ran = []

    async def _handle(*a, **k):
        ran.append(a)

    async def _unpaid(request, cost_credits=1):
        raise HTTPException(status_code=402, detail="Payment required")

    monkeypatch.setattr("api.chat_via_task.handle_chat_via_task_agent", _handle)
    monkeypatch.setattr("api.payment_verification.verify_payment_for_request",
                        _unpaid)
    req = SimpleNamespace(state=SimpleNamespace(user_id="tenant-a"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(MessageRequest(text="hi"), req))
    assert exc.value.status_code == 402
    assert ran == []


# ---------------------------------------------------------------------------
# API13/14/15 — /v1 content parts, stream errors, stream usage
# ---------------------------------------------------------------------------

def _v1_client(monkeypatch, reply="hello from rob", boom=False):
    from fastapi import Request

    import api.openai_compat.router as r

    calls = []

    class _Agent:
        async def chat_once(self, user_id, text, chat_id=None, provider=None,
                            model=None):
            calls.append(text)
            if boom:
                raise RuntimeError("provider down at /secret/path")
            return reply

    class _C:
        def get_agent(self, name):
            return _Agent()

    async def _ok(request, cost_credits=1):
        return ("admin_bypass", {})

    monkeypatch.setattr(r, "verify_payment_for_request", _ok)
    monkeypatch.setattr(r, "_get_container", lambda: _C())
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.user_id = "u1"
        return await call_next(request)

    app.include_router(r.router)
    return TestClient(app, raise_server_exceptions=False), calls


def test_api13_content_part_arrays_are_accepted(monkeypatch):
    client, calls = _v1_client(monkeypatch)
    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "be brief"}]},
            {"role": "assistant", "content": None},
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"}]},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert calls == ["hello\nworld"]


def test_api13_null_content_is_accepted():
    from api.openai_compat.models import ChatCompletionRequest

    req = ChatCompletionRequest(model="m", messages=[
        {"role": "assistant", "content": None},
        {"role": "user", "content": "hi"}])
    assert req.messages[0].content == ""


def test_api13_an_image_part_is_refused_not_dropped(monkeypatch):
    client, calls = _v1_client(monkeypatch)
    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}],
    })
    assert resp.status_code == 400, resp.text
    assert "image_url" in resp.text
    assert calls == []


def test_api14_a_stream_error_is_an_error_frame_not_a_reply(monkeypatch):
    import json

    client, _ = _v1_client(monkeypatch, boom=True)
    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    frames = [line[6:] for line in resp.text.splitlines()
              if line.startswith("data: ")]
    assert frames[-1] == "[DONE]"
    payloads = [json.loads(f) for f in frames[:-1]]
    errors = [p for p in payloads if "error" in p]
    assert len(errors) == 1 and errors[0]["error"]["message"]
    assert "/secret/path" not in resp.text
    # No assistant content and no `finish_reason: stop` claims success.
    for p in payloads:
        for ch in p.get("choices", []):
            assert ch.get("finish_reason") != "stop"
            assert "content" not in ch.get("delta", {})


def test_api15_include_usage_emits_a_usage_chunk(monkeypatch):
    import json

    client, _ = _v1_client(monkeypatch)
    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "hi"}]})
    frames = [line[6:] for line in resp.text.splitlines()
              if line.startswith("data: ")]
    assert frames[-1] == "[DONE]"
    last = json.loads(frames[-2])
    assert last["choices"] == []
    assert last["usage"]["total_tokens"] >= last["usage"]["completion_tokens"] > 0


def test_api15_no_usage_chunk_unless_asked(monkeypatch):
    client, _ = _v1_client(monkeypatch)
    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert '"usage"' not in resp.text


# ---------------------------------------------------------------------------
# API16 — a failed session creation names a reference, never the exception
# ---------------------------------------------------------------------------

def test_api16_session_creation_failure_hides_the_exception(monkeypatch):
    from fastapi import Request

    import api.task_http_api as t

    class _Agent:
        async def create_session(self, *a, **k):
            raise RuntimeError("disk full at /secret/path/bot.db")

    async def _paid(request, cost_credits=1):
        return "admin_bypass", {}

    monkeypatch.setattr("api.payment_verification.verify_payment_for_request",
                        _paid)
    monkeypatch.setattr(t, "_no_model", lambda agent: False)
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request.state.user_id = "u1"
        request.state.authenticated = True
        return await call_next(request)

    app.include_router(t.router, prefix="/api")
    app.dependency_overrides[t.get_task_agent] = lambda: _Agent()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/task/sessions", json={"task": "x",
                                                   "auto_start": False})
    assert resp.status_code == 500, resp.text
    assert "/secret/path" not in resp.text and "disk full" not in resp.text
    assert "reference" in resp.text


# ---------------------------------------------------------------------------
# API18 — an upload never writes through a (dangling) symlink
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api18_a_dangling_symlink_is_not_followed(tmp_path):
    from api.upload_store import store_upload as _store_upload

    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    (ws / "doc.txt").symlink_to(outside)  # dangling: outside does not exist
    written = await _store_upload(ws, "doc.txt", b"payload")
    assert not outside.exists()
    assert written.parent == ws and written.name != "doc.txt"
    assert written.read_bytes() == b"payload"
    assert (ws / "doc.txt").is_symlink()


@pytest.mark.asyncio
async def test_api18_an_existing_file_is_never_clobbered(tmp_path):
    from api.upload_store import store_upload as _store_upload

    (tmp_path / "a.txt").write_bytes(b"old")
    first = await _store_upload(tmp_path, "a.txt", b"new")
    second = await _store_upload(tmp_path, "a.txt", b"newer")
    assert (tmp_path / "a.txt").read_bytes() == b"old"
    assert first != second
    assert first.read_bytes() == b"new" and second.read_bytes() == b"newer"


# ---------------------------------------------------------------------------
# API17 — SSE frames carry no exception text; the poll loop has a lifetime
# ---------------------------------------------------------------------------

class _StreamHandler:
    def __init__(self, *, boom=None, status="running"):
        self.boom = boom
        self.status = status

    async def get_task(self, task_id, history_length=None, **kw):
        from api.a2a.models import A2ATask, A2ATaskState, A2ATaskStatus
        if self.boom:
            raise self.boom
        return A2ATask(id=task_id, status=A2ATaskStatus(
            state=A2ATaskState.WORKING))

    def _get_task_agent(self):
        handler = self

        class _A:
            async def get_session_by_id(self, task_id):
                return {"user_id": "u1", "status": handler.status}

        return _A()


async def _collect(gen):
    return [frame async for frame in gen]


@pytest.mark.asyncio
async def test_api17_an_sse_error_frame_hides_the_exception():
    from api.a2a.streaming import task_event_stream

    h = _StreamHandler(boom=RuntimeError("db at /secret/path/bot.db"))
    frames = await _collect(task_event_stream("t1", h))
    text = "".join(frames)
    assert "/secret/path" not in text and "reference" in text


@pytest.mark.asyncio
async def test_api17_a_not_found_is_still_named():
    from api.a2a.streaming import task_event_stream

    h = _StreamHandler(boom=ValueError("Task t1 not found"))
    text = "".join(await _collect(task_event_stream("t1", h)))
    assert "Task t1 not found" in text


@pytest.mark.asyncio
async def test_api17_the_poll_loop_ends_at_its_lifetime(monkeypatch, tmp_path):
    import asyncio

    import api.a2a.streaming as st

    monkeypatch.setattr(st, "STREAM_MAX_SECONDS", 0.05)
    monkeypatch.setattr(st, "STREAM_POLL_SECONDS", 0.01)

    class _PM:
        def get_subdir(self, *a, **k):
            return tmp_path / "feed"

    monkeypatch.setattr("agents.task.path.pm", lambda: _PM())
    frames = await asyncio.wait_for(
        _collect(st.task_event_stream("t1", _StreamHandler())), timeout=5)
    assert frames  # the initial status, then a clean close
    assert "event: error" not in "".join(frames)


# ---------------------------------------------------------------------------
# API19 — CORS is the OUTERMOST layer
# ---------------------------------------------------------------------------

def test_api19_cors_is_outermost(real_app):
    names = [m.cls.__name__ for m in real_app.app.user_middleware]
    assert names[0] == "CORSMiddleware", names


def test_api19_a_preflight_is_not_401(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_SECRET", "s" * 40)
    monkeypatch.setenv("JWT_SECRET_KEY", "j" * 40)
    monkeypatch.setenv("API_AUTH_TOKEN", "operator-token")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example")
    from api.app import create_app
    client = TestClient(create_app(), raise_server_exceptions=False)
    resp = client.options("/api/task/sessions", headers={
        "Origin": "https://app.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Authorization"})
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("access-control-allow-origin") == "https://app.example"
    # An auth refusal still carries the CORS header, so the browser can read it.
    resp = client.get("/api/task/sessions",
                      headers={"Origin": "https://app.example"})
    assert resp.status_code == 401
    assert resp.headers.get("access-control-allow-origin") == "https://app.example"
