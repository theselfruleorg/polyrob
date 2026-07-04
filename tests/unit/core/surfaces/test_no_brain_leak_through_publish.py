"""Regression lock (P1a top risk): NO brain-state shape may survive MessageRouter.publish.

Every leak shape from modules/llm/brain_scrubber.py's docstring is fed through the
unified outbound seam; the subscribed surface must receive scrubbed text. This is the
single most expensive known live bug (OR-7/B4) — a second un-scrubbed outbound path
would re-create it on a new surface.
"""
import pytest
from core.surfaces.message_router import MessageRouter
from core.surfaces.session_chat_registry import SessionChatRegistry
from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities
from core.surfaces.surface import Surface


class _Capture(Surface):
    def __init__(self):
        super().__init__()
        self.sent = []

    @property
    def surface_id(self):
        return "telegram"

    @property
    def capabilities(self):
        return SurfaceCapabilities(supports_streaming=True)

    async def send(self, msg):
        self.sent.append(msg)
        return SendResult(success=True)

    async def start(self, c):
        pass

    async def stop(self):
        pass


@pytest.fixture
def router_and_surface(tmp_path):
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    reg.bind("k1", "sess_1", "u1", "telegram", "555")
    r = MessageRouter(reg)
    surf = _Capture()
    r.subscribe("telegram", surf)
    return r, surf


_BRAIN = '{"current_state": {"next_goal": "x"}, "memory": "y", "reasoning": "z"}'

# Kimi shape: a brain object trailed by tool-call junk (the documented
# "{brain} + trailing tool-call junk" leak). Bare control tokens WITHOUT a brace
# are an adapter-layer concern (openrouter_client.recover_kimi_content), applied
# before content reaches publish — out of scope for the brain-JSON publish scrub.
LEAK_SHAPES = [
    ("fenced", f"Sure, here you go.\n```json\n{_BRAIN}\n```"),
    ("mixed_blob", f"{_BRAIN} The real answer is 42."),
    ("trailing", f"The real answer is 42. {_BRAIN}"),
    ("kimi_brain_plus_tokens", f"Answer here. {_BRAIN}<|tool_call_begin|>junk<|tool_call_end|>"),
]


@pytest.mark.parametrize("name,leak", LEAK_SHAPES, ids=[s[0] for s in LEAK_SHAPES])
@pytest.mark.asyncio
async def test_no_brain_shape_survives_publish(router_and_surface, name, leak):
    r, surf = router_and_surface
    await r.publish(OutboundMessage(session_key="k1", text=leak, partial=False))
    assert len(surf.sent) == 1
    out = surf.sent[0].text
    assert "current_state" not in out
    assert "next_goal" not in out
    assert "tool_call_begin" not in out


@pytest.mark.asyncio
async def test_genuine_prose_is_untouched(router_and_surface):
    r, surf = router_and_surface
    prose = "Here is a normal answer with a JSON example: {\"price\": 5}."
    await r.publish(OutboundMessage(session_key="k1", text=prose, partial=False))
    assert surf.sent[0].text == prose
