"""044 T15: what a ROOM delivery costs and what it may carry.

`[SILENT]` is the agent's "nothing here needs an answer" — in a room that has to
cost NO message, or an `active` judgement of silence is a public non-sequitur.
And a room is many humans, so a secret shape that survived every other scrub must
not be the thing the agent posts publicly.
"""
import pytest

from core.surfaces.envelopes import (
    OutboundMessage,
    SendResult,
    SurfaceCapabilities,
)
from core.surfaces.message_router import MessageRouter
from core.surfaces.session_chat_registry import SessionChatRegistry
from core.surfaces.surface import Surface

_ROOM_KEY = "agent:main:telegram:group:-1"
_DM_KEY = "k1"


class _RecordingSurface(Surface):
    def __init__(self):
        super().__init__()
        self.sent = []

    @property
    def surface_id(self):
        return "telegram"

    @property
    def capabilities(self):
        return SurfaceCapabilities(supports_streaming=False)

    async def send(self, msg):
        self.sent.append(msg)
        return SendResult(success=True)

    async def start(self, container):
        pass

    async def stop(self):
        pass


@pytest.fixture
def rig(tmp_path):
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind(_DM_KEY, "sess_1", "u_abc", "telegram", "555")
    reg.bind(_ROOM_KEY, "sess_room", "u_owner", "telegram", "-1")
    r = MessageRouter(reg)
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    return r, surf


@pytest.mark.asyncio
async def test_silent_token_is_not_delivered_to_a_room(rig):
    r, surf = rig
    await r.publish(OutboundMessage(session_key=_ROOM_KEY, text="[SILENT]", partial=False))
    assert surf.sent == []


@pytest.mark.asyncio
async def test_silent_token_is_case_and_whitespace_tolerant(rig):
    r, surf = rig
    await r.publish(OutboundMessage(session_key=_ROOM_KEY, text="  [silent]\n",
                                    partial=False))
    assert surf.sent == []


@pytest.mark.asyncio
async def test_a_real_answer_that_mentions_the_token_is_still_delivered(rig):
    """Only an EXACT `[SILENT]` is silence (044 §4.4). Cron's looser "anywhere"
    rule would let one quoted token swallow a genuine public answer."""
    r, surf = rig
    await r.publish(OutboundMessage(session_key=_ROOM_KEY,
                                    text="reply with [SILENT] when there is nothing to say",
                                    partial=False))
    assert len(surf.sent) == 1


@pytest.mark.asyncio
async def test_the_silent_token_still_reaches_a_dm(rig):
    """A DM is one reader who asked — this rule is about not speaking into a
    room full of people, not about hiding a reply from the owner."""
    r, surf = rig
    await r.publish(OutboundMessage(session_key=_DM_KEY, text="[SILENT]", partial=False))
    assert len(surf.sent) == 1


@pytest.mark.asyncio
async def test_room_publish_scrubs_secret_shapes(rig):
    r, surf = rig
    await r.publish(OutboundMessage(
        session_key=_ROOM_KEY,
        text="here you go: sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        partial=False))
    assert len(surf.sent) == 1
    assert "sk-ant-api03" not in surf.sent[0].text
    assert "here you go:" in surf.sent[0].text


@pytest.mark.asyncio
async def test_an_ordinary_room_reply_is_unchanged(rig):
    r, surf = rig
    await r.publish(OutboundMessage(session_key=_ROOM_KEY, text="Base and Solana.",
                                    partial=False))
    assert [m.text for m in surf.sent] == ["Base and Solana."]


@pytest.mark.asyncio
async def test_the_room_scrub_keeps_the_reply_anchor(rig):
    """The scrub rebuilds the message — dropping `reply_to` would unthread every
    room reply that happened to contain a secret shape."""
    r, surf = rig
    await r.publish(OutboundMessage(
        session_key=_ROOM_KEY, text="key sk-ant-api03-" + "B" * 40,
        partial=False, reply_to="77"))
    assert surf.sent[0].reply_to == "77"


class _Queue:
    def __init__(self):
        self.rows = []

    def enqueue(self, **kw):
        self.rows.append(kw)


@pytest.mark.asyncio
async def test_the_durable_queue_payload_is_scrubbed_too(rig, monkeypatch):
    """`scrubbed`, not `msg.text`, is what the durable path enqueues — a stale
    copy would redact the direct send and deliver the secret through the queue."""
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    r, surf = rig
    q = _Queue()
    r.attach_queue(q)
    await r.publish(OutboundMessage(
        session_key=_ROOM_KEY, text="key sk-ant-api03-" + "C" * 40, partial=False))
    assert len(q.rows) == 1, q.rows
    assert "sk-ant-api03" not in q.rows[0]["payload"]
    assert surf.sent == []


@pytest.mark.asyncio
async def test_the_durable_queue_never_carries_a_silent_room_reply(rig, monkeypatch):
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    r, _surf = rig
    q = _Queue()
    r.attach_queue(q)
    await r.publish(OutboundMessage(session_key=_ROOM_KEY, text="[SILENT]", partial=False))
    assert q.rows == []
