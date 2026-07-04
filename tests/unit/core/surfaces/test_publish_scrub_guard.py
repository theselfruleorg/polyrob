"""P1b-1: publish must DROP a wholly-brain (empty-after-scrub) chunk.

F3 divergence: HITLManager.stream_output (the legacy funnel) returns early when
scrub_brain_blocks(chunk) is None or strips to "" (hitl_manager.py:254) -> a
wholly-brain telemetry chunk never reaches the user. message_router.publish had no
such guard, so the same chunk would surface.send("") -> an empty bubble on every
surface. This locks publish to the SAME drop behavior, and asserts scrub-equivalence:
the bytes a surface receives via publish equal what the legacy scrub produced.
"""
import pytest

from core.surfaces.message_router import MessageRouter
from core.surfaces.session_chat_registry import SessionChatRegistry
from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities
from core.surfaces.surface import Surface
from modules.llm.brain_scrubber import scrub_brain_blocks


class _RecordingSurface(Surface):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.streamed = []

    @property
    def surface_id(self):
        return "telegram"

    @property
    def capabilities(self):
        return SurfaceCapabilities(supports_streaming=True)

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
def wired(tmp_path):
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind("k1", "sess_1", "u_abc", "telegram", "555")
    router = MessageRouter(reg)
    surf = _RecordingSurface()
    router.subscribe("telegram", surf)
    return router, surf


# A chunk that is WHOLLY brain-state telemetry (scrubs to "").
_ALL_BRAIN = '{"current_state": {"next_goal": "do x", "evaluation_previous_goal": "ok"}, "memory": "y"}'


@pytest.mark.asyncio
async def test_publish_drops_wholly_brain_discrete(wired):
    router, surf = wired
    assert scrub_brain_blocks(_ALL_BRAIN).strip() == ""  # precondition: it IS all brain
    await router.publish(OutboundMessage(session_key="k1", text=_ALL_BRAIN, partial=False))
    assert surf.sent == []  # dropped, not an empty bubble


@pytest.mark.asyncio
async def test_publish_drops_wholly_brain_partial(wired):
    router, surf = wired
    await router.publish(OutboundMessage(session_key="k1", text=_ALL_BRAIN, partial=True, stream_id="s1"))
    assert surf.streamed == []  # dropped


@pytest.mark.asyncio
async def test_publish_drops_empty_text(wired):
    router, surf = wired
    await router.publish(OutboundMessage(session_key="k1", text="", partial=False))
    assert surf.sent == []


@pytest.mark.asyncio
async def test_publish_passes_real_prose(wired):
    router, surf = wired
    await router.publish(OutboundMessage(session_key="k1", text="The answer is 42.", partial=False))
    assert len(surf.sent) == 1
    assert surf.sent[0].text == "The answer is 42."


@pytest.mark.asyncio
async def test_scrub_equivalence_mixed(wired):
    """Mixed prose+brain: surface receives exactly the legacy scrub output."""
    router, surf = wired
    mixed = 'Here is the answer. {"current_state": {"next_goal": "x"}, "memory": "y"}'
    await router.publish(OutboundMessage(session_key="k1", text=mixed, partial=False))
    assert len(surf.sent) == 1
    assert surf.sent[0].text == scrub_brain_blocks(mixed)
    assert "current_state" not in surf.sent[0].text
    assert "Here is the answer." in surf.sent[0].text
