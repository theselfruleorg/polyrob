"""C1: the /api/chat/message endpoint must derive user_id from the authenticated
request state (get_authenticated_user_id) and MUST NOT let a client-supplied body
`user_id` override it — otherwise any authenticated caller can bill/recall memory as
another tenant by POSTing {"text": "...", "user_id": "<victim>"}.

Exercises the REAL endpoint closure (pulled from the app routes), not a copy.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from api.app import create_app, app_state
from api.models import MessageRequest, MessageResponse


def _chat_endpoint():
    app = create_app()
    for r in app.routes:
        if getattr(r, "path", None) == "/api/chat/message":
            return r.endpoint
    raise AssertionError("/api/chat/message endpoint not registered")


def test_body_user_id_cannot_override_authenticated_identity(monkeypatch):
    endpoint = _chat_endpoint()
    # Truthy container so we pass the 503 guard and reach user_id resolution.
    app_state["container"] = MagicMock()

    captured = {}

    async def fake_handle(container, user_id, text, chat_id):
        captured["user_id"] = user_id
        captured["chat_id"] = chat_id
        return MessageResponse(success=True, text="ok")

    monkeypatch.setattr("api.chat_via_task.handle_chat_via_task_agent", fake_handle)

    # Authenticated identity is tenant-a; attacker supplies tenant-b in the body.
    req = SimpleNamespace(state=SimpleNamespace(user_id="tenant-a"))
    body = MessageRequest(text="hi", user_id="tenant-b", chat_id=None)

    out = asyncio.run(endpoint(body, req))

    assert isinstance(out, MessageResponse)
    assert out.success is True
    # The authenticated identity wins; the body user_id is ignored.
    assert captured["user_id"] == "tenant-a"
    # chat_id defaults to the authenticated user_id, never the body user_id.
    assert captured["chat_id"] == "tenant-a"
