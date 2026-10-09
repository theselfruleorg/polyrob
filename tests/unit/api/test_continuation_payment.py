"""Continuation is a new compute turn, including on pre-restriction sessions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.models import UserMessage


def request():
    req = Request({"type": "http", "method": "POST", "path": "/messages", "headers": []})
    req.state.user_id = "u"
    req.state.role = "user"
    return req


@pytest.mark.asyncio
async def test_session_message_rechecks_payment_before_delivering(monkeypatch):
    from api.task_http_api import send_user_message
    admission = AsyncMock(side_effect=HTTPException(402, "Insufficient credits"))
    monkeypatch.setattr("api.payment_verification.verify_payment_for_request", admission)
    agent = SimpleNamespace(get_session_by_id=AsyncMock(return_value={
        "user_id": "u", "status": "running", "billing_limited": True}))
    with pytest.raises(HTTPException) as error:
        await send_user_message("session", UserMessage(text="continue"), request(), agent)
    assert error.value.status_code == 402
    admission.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_session_refused_before_message_or_attachment_processing(monkeypatch):
    from api.task_http_api import send_user_message
    from core.billing_context import mark_billed, reset_billed
    monkeypatch.setattr("api.payment_verification.verify_payment_for_request", AsyncMock())
    agent = SimpleNamespace(get_session_by_id=AsyncMock(return_value={
        "user_id": "u", "status": "running"}))
    token = mark_billed()
    try:
        with pytest.raises(HTTPException) as error:
            await send_user_message("session", UserMessage(text="continue"), request(), agent)
        assert error.value.status_code == 409
        assert "create a new session" in error.value.detail
    finally:
        reset_billed(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a2a_continuations_cannot_skip_admission(monkeypatch, stream):
    from api.a2a import endpoints, streaming
    from api.a2a.models import A2AMessage, SendMessageRequest
    from core.container import DependencyContainer
    handler = SimpleNamespace(send_message=AsyncMock())
    denied = AsyncMock(side_effect=HTTPException(402, "Insufficient credits"))
    message = A2AMessage(role="user", parts=[{"kind": "text", "text": "continue"}], taskId="old")
    if stream:
        monkeypatch.setattr("api.payment_verification.verify_payment_for_request", denied)
        monkeypatch.setattr(streaming, "get_user_permissive", AsyncMock(return_value="u"))
        monkeypatch.setattr(streaming, "A2ATaskHandler", lambda _: handler)
        monkeypatch.setattr(DependencyContainer, "get_instance", lambda: None)
        call = streaming.stream_message(SendMessageRequest(message=message), request())
    else:
        monkeypatch.setattr(endpoints, "verify_payment_for_request", denied)
        call = endpoints._handle_rpc_method("message/send", {"message": message.model_dump()},
                                            "u", handler, request())
    with pytest.raises(HTTPException) as error:
        await call
    assert error.value.status_code == 402
    denied.assert_awaited_once()
    handler.send_message.assert_not_awaited()
