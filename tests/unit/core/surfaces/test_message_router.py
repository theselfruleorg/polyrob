import pytest
from core.surfaces.message_router import MessageRouter
from core.surfaces.session_chat_registry import SessionChatRegistry
from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities
from core.surfaces.surface import Surface


class _RecordingSurface(Surface):
    def __init__(self, streaming=True):
        super().__init__()
        self._streaming = streaming
        self.sent = []
        self.streamed = []

    @property
    def surface_id(self):
        return "telegram"

    @property
    def capabilities(self):
        return SurfaceCapabilities(supports_streaming=self._streaming)

    async def send(self, msg):
        self.sent.append(msg)
        return SendResult(success=True)

    async def start(self, container):
        pass

    async def stop(self):
        pass

    async def stream(self, msg):
        self.streamed.append(msg)


@pytest.fixture
def router(tmp_path):
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind("k1", "sess_1", "u_abc", "telegram", "555")
    return MessageRouter(reg), reg


@pytest.mark.asyncio
async def test_publish_routes_discrete_to_send(router):
    r, _ = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    await r.publish(OutboundMessage(session_key="k1", text="done", partial=False))
    assert len(surf.sent) == 1 and surf.sent[0].text == "done"


@pytest.mark.asyncio
async def test_publish_routes_partial_to_stream(router):
    r, _ = router
    surf = _RecordingSurface(streaming=True)
    r.subscribe("telegram", surf)
    await r.publish(OutboundMessage(session_key="k1", text="Hel", partial=True, stream_id="s1"))
    assert len(surf.streamed) == 1


@pytest.mark.asyncio
async def test_publish_scrubs_brain_state(router):
    r, _ = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    leak = 'Here is the answer. {"current_state": {"next_goal": "x"}, "memory": "y"}'
    await r.publish(OutboundMessage(session_key="k1", text=leak, partial=False))
    assert "current_state" not in surf.sent[0].text
    assert "Here is the answer." in surf.sent[0].text


@pytest.mark.asyncio
async def test_publish_unknown_key_is_failopen_noop(router):
    r, _ = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    await r.publish(OutboundMessage(session_key="UNKNOWN", text="x", partial=False))
    assert surf.sent == []  # no crash, nothing delivered


@pytest.mark.asyncio
async def test_publish_raising_surface_is_failopen(router):
    r, _ = router

    class _Boom(_RecordingSurface):
        async def send(self, msg):
            raise RuntimeError("surface down")

    r.subscribe("telegram", _Boom())
    # Must not raise
    await r.publish(OutboundMessage(session_key="k1", text="x", partial=False))


@pytest.mark.asyncio
async def test_send_message_backcompat_shim(router):
    r, reg = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    await r.send_message(chat_id="555", text="cron note", surface_id="telegram")
    assert len(surf.sent) == 1 and surf.sent[0].text == "cron note"
