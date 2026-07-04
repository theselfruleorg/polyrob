"""Canonical /api/chat/message handler — chat served purely by TaskAgent.chat_once
(legacy ChatAgent retired, HANDOFF-C). Always returns a MessageResponse; graceful
MessageResponse(success=False) on failure (no legacy fallback)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from api.chat_via_task import handle_chat_via_task_agent
from api.models import MessageResponse


def _container(reply=None, raises=None):
    ta = MagicMock()
    if raises is not None:
        ta.chat_once = AsyncMock(side_effect=raises)
    else:
        ta.chat_once = AsyncMock(return_value=reply)
    container = MagicMock()
    container.get_agent.return_value = ta
    return container, ta


def test_returns_reply_on_success():
    container, ta = _container(reply="Hello from Rob")
    out = asyncio.run(handle_chat_via_task_agent(container, "u1", "hello", "c1"))
    assert isinstance(out, MessageResponse)
    assert out.success is True
    assert out.text == "Hello from Rob"
    ta.chat_once.assert_awaited_once_with(user_id="u1", text="hello", chat_id="c1")


def test_graceful_error_on_exception():
    container, ta = _container(raises=RuntimeError("boom"))
    out = asyncio.run(handle_chat_via_task_agent(container, "u1", "hello", "c1"))
    assert isinstance(out, MessageResponse)
    assert out.success is False  # no legacy fallback — graceful failure
    assert out.text  # user-safe, non-empty


def test_graceful_when_agent_unavailable():
    container = MagicMock()
    container.get_agent.return_value = None
    out = asyncio.run(handle_chat_via_task_agent(container, "u1", "hello", "c1"))
    assert isinstance(out, MessageResponse)
    assert out.success is False


def test_graceful_when_no_container():
    out = asyncio.run(handle_chat_via_task_agent(None, "u1", "hello", "c1"))
    assert isinstance(out, MessageResponse)
    assert out.success is False
