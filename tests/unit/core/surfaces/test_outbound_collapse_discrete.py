import pytest
from core.surfaces.message_router import MessageRouter
from core.surfaces.session_chat_registry import SessionChatRegistry
from core.surfaces.outbound_mirror import build_discrete_publish
from core.surfaces.envelopes import MessageKind


@pytest.mark.asyncio
async def test_discrete_publishes_committed_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    reg.bind("k1", "sess_1", "u1", "cli", "local")
    published = []

    class _R(MessageRouter):
        async def publish(self, msg):
            published.append(msg)

    fn = build_discrete_publish(_R(reg), session_key="k1")
    await fn(text="done")
    assert len(published) == 1
    assert published[0].partial is False
    assert published[0].text == "done"
    assert published[0].kind == MessageKind.AGENT_TEXT


@pytest.mark.asyncio
async def test_no_publish_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("SINGULAR_CHAT_ENABLED", raising=False)
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    published = []

    class _R(MessageRouter):
        async def publish(self, msg):
            published.append(msg)

    fn = build_discrete_publish(_R(reg), session_key="k1")
    await fn(text="done")
    assert published == []


@pytest.mark.asyncio
async def test_no_router_or_key_is_noop(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    await build_discrete_publish(None, "k1")(text="x")  # must not raise
    # also no key
    reg_published = []
    await build_discrete_publish(object(), None)(text="x")
    assert reg_published == []
