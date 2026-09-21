"""Regression tests for the 2026-09-21 interface deep-audit, section B (api/).

One test (or small cluster) per finding, named by its id so a future reader can
trace the assertion back to the defect it pins.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# B1 — a settled x402 payment that then 401/403s is refundable
# ---------------------------------------------------------------------------

def test_b1_refund_due_on_auth_refusal_after_settlement():
    from modules.x402.x402_integration import should_refund_on_status

    # The payment WAS the credential; an auth gate refusing it afterwards took
    # money for nothing.
    assert should_refund_on_status(401) is True
    assert should_refund_on_status(403) is True
    # Server errors were already refundable.
    assert should_refund_on_status(500) is True
    assert should_refund_on_status(503) is True
    # A caller's own malformed request is not refundable here.
    assert should_refund_on_status(200) is False
    assert should_refund_on_status(400) is False
    assert should_refund_on_status(404) is False
    assert should_refund_on_status(429) is False


# ---------------------------------------------------------------------------
# B2 — the rob_xxx validator is always-on and never refuses
# ---------------------------------------------------------------------------

def test_b2_api_key_shape_recognition():
    from api.api_key_auth import looks_like_api_key

    assert looks_like_api_key("rob_" + "a" * 40)
    assert not looks_like_api_key("rob_short")
    assert not looks_like_api_key("some-operator-service-token-value-here")
    assert not looks_like_api_key(None)


def test_b2_middleware_never_refuses_an_unknown_key():
    """It only ADDS an identity; refusal stays the downstream gates' job, so
    mounting it unconditionally cannot break a request that worked before."""
    from api.api_key_auth import APIKeyAuthMiddleware

    app = FastAPI()

    @app.get("/probe")
    async def probe():
        return {"ok": True}

    app.add_middleware(APIKeyAuthMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/probe", headers={"X-API-KEY": "rob_" + "z" * 40})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# B3 — x402 only settles on a route that actually charges
# ---------------------------------------------------------------------------

def test_b3_only_gated_routes_are_settled():
    from modules.x402.middleware import X402PaymentMiddleware

    mw = X402PaymentMiddleware.__new__(X402PaymentMiddleware)
    assert mw._is_x402_gated("/a2a/rpc", "POST")
    assert mw._is_x402_gated("/v1/chat/completions", "POST")
    # A free read must never be settled just because a header was present.
    assert not mw._is_x402_gated("/a2a/tasks/abc", "GET")
    assert not mw._is_x402_gated("/api/task/sessions", "POST")


# ---------------------------------------------------------------------------
# B4 — the service token is its own role, and the oracle is gone
# ---------------------------------------------------------------------------

def test_b4_service_role_is_not_admin():
    from api.auth_constants import SERVICE_ROLE, is_admin_role, is_service_role

    assert is_service_role(SERVICE_ROLE)
    assert not is_admin_role(SERVICE_ROLE)


def test_b4_test_auth_oracle_is_deleted():
    import api.app as app_module

    src = open(app_module.__file__).read()
    assert '@app.get("/api/test-auth")' not in src


# ---------------------------------------------------------------------------
# B6 — /health is derived from signals that are actually written
# ---------------------------------------------------------------------------

def test_b6_dead_limiter_is_gone():
    from api.app import app_state

    assert "active_updates" not in app_state
    assert "update_semaphore" not in app_state


# ---------------------------------------------------------------------------
# B9 — status mapping is complete and UNKNOWN is not terminal
# ---------------------------------------------------------------------------

def test_b9_every_session_status_maps():
    from agents.task.agent.session import SessionStatus
    from api.a2a.task_handler import ROB_TO_A2A_STATE

    for status in SessionStatus:
        assert status.value in ROB_TO_A2A_STATE, status.value
    # The dead row is gone.
    assert "error" not in ROB_TO_A2A_STATE


def test_b9_unknown_is_not_terminal():
    from api.a2a.models import A2ATaskState

    assert not A2ATaskState.is_terminal(A2ATaskState.UNKNOWN)
    assert A2ATaskState.is_terminal(A2ATaskState.COMPLETED)
    assert A2ATaskState.is_terminal(A2ATaskState.FAILED)


def test_b9_initializing_is_working():
    from api.a2a.models import A2ATaskState
    from api.a2a.task_handler import ROB_TO_A2A_STATE

    assert ROB_TO_A2A_STATE["initializing"] is A2ATaskState.WORKING


# ---------------------------------------------------------------------------
# B10 — push URL goes through the shared SSRF validator; token is encrypted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://example.com/hook",          # plaintext
    "https://127.0.0.1/hook",           # loopback
    "https://169.254.169.254/latest",   # cloud metadata
    "https://10.0.0.5/hook",            # RFC1918
    "https://localhost/hook",
    "",
])
def test_b10_unsafe_push_urls_are_refused(url):
    from api.a2a.task_handler import validate_push_url

    with pytest.raises(ValueError):
        validate_push_url(url)


def test_b10_push_token_round_trips_encrypted(monkeypatch, tmp_path):
    from api.a2a.task_handler import _decrypt_push_token, _encrypt_push_token

    monkeypatch.setenv("MCP_ENCRYPTION_KEY", _fernet_key())
    stored = _encrypt_push_token("s3cr3t")
    assert stored is not None
    # The stored form must not carry the plaintext.
    assert "s3cr3t" not in stored
    assert stored.startswith("fernet:")
    assert _decrypt_push_token(stored) == "s3cr3t"


def test_b10_legacy_plaintext_token_still_readable():
    from api.a2a.task_handler import _decrypt_push_token

    assert _decrypt_push_token("legacy-plaintext") == "legacy-plaintext"


def _fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


# ---------------------------------------------------------------------------
# B14 — the ingress body cap is derived from the ONE upload cap
# ---------------------------------------------------------------------------

def test_b14_body_limit_covers_the_advertised_upload_cap():
    from core.surfaces.inbound_attachments import upload_max_mb
    from api.request_limits import max_body_bytes

    assert max_body_bytes() > int(upload_max_mb() * 1024 * 1024)


# ---------------------------------------------------------------------------
# B16 — trustMode onchain requires something a reader can look up
# ---------------------------------------------------------------------------

def test_b16_onchain_flag_alone_does_not_claim_onchain(monkeypatch):
    import modules.eip8004.registration as reg

    monkeypatch.setenv("EIP8004_ONCHAIN_ENABLED", "true")
    monkeypatch.delenv("EIP8004_AGENT_ID", raising=False)
    monkeypatch.delenv("EIP8004_IDENTITY_REGISTRY", raising=False)
    monkeypatch.setattr(
        "core.instance.load_erc8004_record", lambda *a, **k: None, raising=False)

    doc = reg.build_registration_file("https://example.test")
    assert doc.trustMode == "local"
    assert not (doc.registrations or [])


# ---------------------------------------------------------------------------
# B22 / B23 — the x402 advertisement is derived, not asserted
# ---------------------------------------------------------------------------

def test_b22_supported_assets_are_only_what_the_rail_settles():
    from api.x402_advertisement import supported_assets, supported_chains

    assets = supported_assets()
    assert assets == ["usdc"], assets
    # No ethereum mainnet row on the facilitator rail.
    assert "ethereum" not in supported_chains()


def test_b23_credits_block_is_observed(monkeypatch):
    import api.x402_advertisement as adv

    monkeypatch.setattr(adv, "credits_enabled", lambda: False)
    block = adv.credits_block()
    assert block["enabled"] is False
    assert "credit_cost_usd" not in block


# ---------------------------------------------------------------------------
# B24 — one payment-required code
# ---------------------------------------------------------------------------

def test_b24_single_payment_required_code():
    from api.a2a.endpoints import A2A_ERROR_PAYMENT_REQUIRED
    from api.a2a.models import A2AErrorCode

    assert A2A_ERROR_PAYMENT_REQUIRED == A2AErrorCode.PAYMENT_REQUIRED


# ---------------------------------------------------------------------------
# B25 — the A2A error codes are reachable
# ---------------------------------------------------------------------------

def test_b25_status_and_value_error_mapping():
    from api.a2a.endpoints import _code_for_status, _code_for_value_error
    from api.a2a.models import A2AErrorCode

    assert _code_for_status(401) == A2AErrorCode.AUTHENTICATION_REQUIRED
    assert _code_for_status(402) == A2AErrorCode.PAYMENT_REQUIRED
    assert _code_for_status(501) == A2AErrorCode.UNSUPPORTED_OPERATION
    assert _code_for_value_error("Task abc not found") == A2AErrorCode.TASK_NOT_FOUND
    assert (_code_for_value_error("Cannot cancel task in terminal state: completed")
            == A2AErrorCode.TASK_NOT_CANCELABLE)
    assert _code_for_value_error("bad param") == A2AErrorCode.INVALID_PARAMS


# ---------------------------------------------------------------------------
# B27 — SSE frames carry the spec fields
# ---------------------------------------------------------------------------

def test_b27_status_frame_is_spec_shaped():
    from api.a2a.models import (A2ATask, A2ATaskState, A2ATaskStatus,
                                TaskStatusUpdateEvent)

    task = A2ATask(id="t1", contextId="c1",
                   status=A2ATaskStatus(state=A2ATaskState.WORKING))
    frame = TaskStatusUpdateEvent.from_task(task, final=False).model_dump()
    assert frame["kind"] == "status-update"
    assert frame["taskId"] == "t1"
    assert frame["contextId"] == "c1"
    assert frame["status"]["state"] == "working"


def test_b27_sse_event_name_is_the_frame_kind():
    from api.a2a.models import (A2ATask, A2ATaskState, A2ATaskStatus,
                                TaskStatusUpdateEvent)
    from api.a2a.streaming import _format_sse_event

    task = A2ATask(id="t1", contextId="c1",
                   status=A2ATaskStatus(state=A2ATaskState.WORKING))
    out = _format_sse_event(TaskStatusUpdateEvent.from_task(task))
    assert out.startswith("event: status-update\n")


# ---------------------------------------------------------------------------
# B34 / B35 — one status vocabulary, honest capabilities
# ---------------------------------------------------------------------------

def test_b34_status_vocabulary_has_no_phantom_status():
    from agents.task.agent.session import SessionStatus
    from api.task_http_api import (RESUMABLE_SESSION_STATUSES,
                                   TERMINAL_SESSION_STATUSES)

    known = {s.value for s in SessionStatus}
    assert TERMINAL_SESSION_STATUSES <= known
    assert RESUMABLE_SESSION_STATUSES <= known
    assert "error" not in TERMINAL_SESSION_STATUSES | RESUMABLE_SESSION_STATUSES
    # SUSPENDED is resumable, never terminal.
    assert "suspended" in RESUMABLE_SESSION_STATUSES
    assert "suspended" not in TERMINAL_SESSION_STATUSES


# ---------------------------------------------------------------------------
# B36 — a forged turn-kind can never arrive over HTTP
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["self_wake", "delegation_result"])
def test_b36_forged_kinds_are_refused(kind):
    from pydantic import ValidationError
    from api.models import UserMessage

    with pytest.raises(ValidationError):
        UserMessage(text="hi", kind=kind)


def test_b36_unknown_kind_is_refused():
    from pydantic import ValidationError
    from api.models import UserMessage

    with pytest.raises(ValidationError):
        UserMessage(text="hi", kind="anything_goes")


@pytest.mark.parametrize("kind", ["guidance", "feedback", "comment",
                                  "continuation", "steer"])
def test_b36_normal_kinds_pass(kind):
    from api.models import UserMessage

    assert UserMessage(text="hi", kind=kind).kind == kind


def test_b36_default_and_empty_are_guidance():
    from api.models import UserMessage

    assert UserMessage(text="hi").kind == "guidance"
    assert UserMessage(text="hi", kind="").kind == "guidance"


def test_b36_exclusion_is_pinned_to_the_forged_turn_seam():
    """Never a second copy of "what counts as a forged turn"."""
    from core.security.forged_turns import FORGED_TURN_KINDS
    from api.models import ALLOWED_USER_MESSAGE_KINDS

    assert FORGED_TURN_KINDS
    assert not (ALLOWED_USER_MESSAGE_KINDS & set(FORGED_TURN_KINDS))


def test_b36_the_console_chat_kind_still_works():
    """webview/server.py sends kind="comment" — the console must not 422."""
    from api.models import UserMessage

    assert UserMessage(text="hi", kind="comment").kind == "comment"


# ---------------------------------------------------------------------------
# B39 — the dead session-create model is gone
# ---------------------------------------------------------------------------

def test_b39_dead_model_deleted():
    import api.models as models

    assert not hasattr(models, "SessionCreateRequest")


# ---------------------------------------------------------------------------
# B41 — the treasury address is resolved once per process
# ---------------------------------------------------------------------------

def test_b41_treasury_address_is_cached(monkeypatch):
    import api.x402_advertisement as adv
    import modules.x402.x402_integration as integ

    adv.reset_treasury_cache()
    calls = []

    def _resolve():
        calls.append(1)
        return "0xTreasury"

    monkeypatch.setattr(integ, "resolve_treasury_address", _resolve)
    assert adv.treasury_address() == "0xTreasury"
    assert adv.treasury_address() == "0xTreasury"
    assert len(calls) == 1
    adv.reset_treasury_cache()


def test_b41_an_empty_address_is_not_cached(monkeypatch):
    """A wallet that is not ready yet must be retried, not remembered as ''."""
    import api.x402_advertisement as adv
    import modules.x402.x402_integration as integ

    adv.reset_treasury_cache()
    monkeypatch.delenv("X402_PAYMENT_ADDRESS", raising=False)
    monkeypatch.setattr(integ, "resolve_treasury_address", lambda: "")
    assert adv.treasury_address() == ""
    monkeypatch.setattr(integ, "resolve_treasury_address", lambda: "0xLater")
    assert adv.treasury_address() == "0xLater"
    adv.reset_treasury_cache()


# ---------------------------------------------------------------------------
# B42 — the advertised base URL never comes from the Host header
# ---------------------------------------------------------------------------

def test_b42_host_header_cannot_set_the_base_url(monkeypatch):
    from api.a2a.agent_card import router

    monkeypatch.setenv("A2A_BASE_URL", "https://real.example")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/.well-known/agent.json",
                      headers={"Host": "evil.example",
                               "X-Forwarded-Proto": "https"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "evil.example" not in str(body)
    assert body["url"] == "https://real.example/a2a"


def test_b42_unconfigured_base_url_is_loopback_not_a_guess(monkeypatch):
    from api.base_url import base_url, is_public_base_url_configured, public_base_url

    monkeypatch.delenv("A2A_BASE_URL", raising=False)
    monkeypatch.delenv("POLYROB_BASE_URL", raising=False)
    assert base_url().startswith("http://localhost:")
    assert public_base_url() is None
    assert is_public_base_url_configured() is False


# ---------------------------------------------------------------------------
# B43 — no exception text in a response; a bad id is 400
# ---------------------------------------------------------------------------

def test_b43_internal_error_does_not_echo_the_exception():
    from api.task_http_api import _internal_error

    exc = _internal_error(RuntimeError("/var/lib/polyrob/secret.db is locked"),
                          "Reading the session status")
    assert exc.status_code == 500
    assert "secret.db" not in exc.detail
    assert "Reading the session status" in exc.detail


def test_b43_bad_session_id_is_400_not_500(monkeypatch):
    from fastapi import HTTPException
    import api.task_http_api as thttp

    class _PM:
        def clean_session_id(self, sid):
            raise ValueError("Security violation: traversal")

    monkeypatch.setattr("agents.task.path.pm", lambda: _PM())
    with pytest.raises(HTTPException) as ei:
        thttp.clean_session_id_at_entry("../../etc/passwd")
    assert ei.value.status_code == 400


# ---------------------------------------------------------------------------
# B44 — the admin check reads the key the validator writes
# ---------------------------------------------------------------------------

def test_b44_admin_wallet_key_is_honoured():
    import asyncio

    from api.middleware import AuthenticationMiddleware

    mw = AuthenticationMiddleware.__new__(AuthenticationMiddleware)

    class _Req:
        class url:
            path = "/api/admin/users"
        method = "GET"

    # `_validate_auth` emits `admin_wallet`, never `wallet_address`.
    assert asyncio.run(mw._check_permissions(_Req(), {"admin_wallet": True}))
    assert asyncio.run(mw._check_permissions(_Req(), {"role": "admin"}))
    assert not asyncio.run(
        mw._check_permissions(_Req(), {"role": "user", "admin_wallet": False}))


# ---------------------------------------------------------------------------
# B8 — the workspace artifact route exists and is confined
# ---------------------------------------------------------------------------

def test_b8_route_is_registered():
    """The artifact URI A2A mints must resolve to a real route.

    Asserted through a mounted app: `task_http_api.router` includes the
    workspace sub-router, and this FastAPI version keeps an included router as
    a lazy node rather than flattening its routes.
    """
    from api.task_http_api import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    paths = set(app.openapi()["paths"])
    assert "/api/task/sessions/{session_id}/workspace/{file_path}" in paths


def test_b8_traversal_and_symlinks_are_refused(tmp_path):
    from fastapi import HTTPException
    from api.task_workspace import resolve_workspace_file

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "report.txt").write_text("hello")
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    (ws / "link.txt").symlink_to(outside)

    # The happy path.
    assert resolve_workspace_file(ws, "report.txt").read_text() == "hello"

    for bad in ["../secret.txt", "/etc/passwd", "a/../../secret.txt", ""]:
        with pytest.raises(HTTPException):
            resolve_workspace_file(ws, bad)

    with pytest.raises(HTTPException) as ei:
        resolve_workspace_file(ws, "link.txt")
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------------
# B18 — the OpenAI-compatible surface refuses what it cannot do
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"tools": [{"type": "function"}]},
    {"tool_choice": "auto"},
    {"response_format": {"type": "json_object"}},
    {"stop": ["\n"]},
    {"n": 2},
])
def test_b18_unsupported_params_are_named(payload):
    from api.openai_compat.models import ChatCompletionRequest

    req = ChatCompletionRequest(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], **payload)
    assert req.unsupported_fields(), payload
    assert req.unsupported_reason()


def test_b18_max_tokens_is_accepted_and_documented():
    from api.openai_compat.models import ACCEPTED_NO_OP_PARAMS, ChatCompletionRequest

    req = ChatCompletionRequest(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}],
        max_tokens=64)
    assert req.unsupported_fields() == []
    assert "ignored" in ACCEPTED_NO_OP_PARAMS["max_tokens"]


def test_b18_usage_is_marked_estimated():
    from api.openai_compat.models import ChatCompletionResponse

    body = ChatCompletionResponse.build(
        id="chatcmpl-1", created=1, model="gpt-4o", reply="hi",
        prompt_tokens=3, completion_tokens=1).model_dump()
    assert body["usage"]["estimated"] is True


def test_b18_error_envelope_is_openai_shaped():
    from api.openai_compat.errors import is_openai_compat_path, openai_error_body

    assert is_openai_compat_path("/v1/chat/completions")
    assert not is_openai_compat_path("/api/task/sessions")
    body = openai_error_body(400, "bad thing")
    assert set(body["error"]) >= {"message", "type", "code", "param"}
    assert body["error"]["type"] == "invalid_request_error"
    assert openai_error_body(401, "x")["error"]["type"] == "authentication_error"
