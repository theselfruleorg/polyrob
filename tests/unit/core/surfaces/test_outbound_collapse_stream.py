import pytest
from core.surfaces.message_router import MessageRouter
from core.surfaces.session_chat_registry import SessionChatRegistry
from agents.task.session.feed import build_stream_publish


@pytest.mark.asyncio
async def test_stream_chunk_publishes_partial_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    reg.bind("agent:main:cli:dm:local:u1", "sess_1", "u1", "cli", "local")
    published = []

    class _R(MessageRouter):
        async def publish(self, msg):
            published.append(msg)

    router = _R(reg)
    fn = build_stream_publish(router, session_key="agent:main:cli:dm:local:u1")
    await fn(chunk="Hello", step=3)
    assert len(published) == 1
    assert published[0].partial is True
    assert published[0].text == "Hello"
    assert published[0].session_key == "agent:main:cli:dm:local:u1"


@pytest.mark.asyncio
async def test_no_publish_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("SINGULAR_CHAT_ENABLED", raising=False)
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    published = []

    class _R(MessageRouter):
        async def publish(self, msg):
            published.append(msg)

    fn = build_stream_publish(_R(reg), session_key="k")
    await fn(chunk="Hello", step=1)
    assert published == []


@pytest.mark.asyncio
async def test_no_router_is_noop(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    fn = build_stream_publish(None, session_key="k")
    # must not raise
    await fn(chunk="Hello", step=1)


@pytest.mark.asyncio
async def test_no_session_key_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    published = []

    class _R(MessageRouter):
        async def publish(self, msg):
            published.append(msg)

    fn = build_stream_publish(_R(reg), session_key=None)
    await fn(chunk="Hello", step=1)
    assert published == []
