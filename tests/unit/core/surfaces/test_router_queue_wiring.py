# tests/unit/core/surfaces/test_router_queue_wiring.py
import os
import pytest

from core.surfaces.message_router import MessageRouter
from core.surfaces.session_chat_registry import SessionChatRegistry
from core.surfaces.outbound_queue import OutboundDeliveryQueue
from core.surfaces.envelopes import OutboundMessage, SendResult


class _Surface:
    surface_id = "wa"

    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg.text)
        return SendResult(success=True)

    async def stream(self, msg):
        ...


@pytest.mark.asyncio
async def test_publish_enqueues_when_queue_attached(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    reg = SessionChatRegistry(os.path.join(tmp_path, "reg.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    r = MessageRouter(reg)
    r.attach_queue(q)  # new seam
    surf = _Surface()
    r.subscribe("wa", surf)
    await r.publish(OutboundMessage(session_key="sk", text="hello"))
    assert surf.sent == []  # NOT sent directly
    assert q.counts()["pending"] == 1  # enqueued


@pytest.mark.asyncio
async def test_publish_direct_when_flag_off(tmp_path, monkeypatch):
    """Flag OFF -> direct send, even if a queue is attached."""
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "false")
    reg = SessionChatRegistry(os.path.join(tmp_path, "reg.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    r = MessageRouter(reg)
    r.attach_queue(q)
    surf = _Surface()
    r.subscribe("wa", surf)
    await r.publish(OutboundMessage(session_key="sk", text="hello"))
    assert surf.sent == ["hello"]  # sent directly
    assert q.counts()["pending"] == 0  # nothing enqueued


@pytest.mark.asyncio
async def test_streaming_delta_always_direct(tmp_path, monkeypatch):
    """partial=True -> always direct stream(), even with flag ON + queue attached."""
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    reg = SessionChatRegistry(os.path.join(tmp_path, "reg.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    r = MessageRouter(reg)
    r.attach_queue(q)
    streamed = []

    class _StreamSurface:
        surface_id = "wa"

        async def send(self, msg):
            return SendResult(success=True)

        async def stream(self, msg):
            streamed.append(msg.text)

    r.subscribe("wa", _StreamSurface())
    await r.publish(OutboundMessage(session_key="sk", text="delta", partial=True))
    assert streamed == ["delta"]  # streaming went direct
    assert q.counts()["pending"] == 0  # nothing enqueued


@pytest.mark.asyncio
async def test_publish_no_queue_attached(tmp_path, monkeypatch):
    """No queue attached -> always direct, even if flag ON."""
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    reg = SessionChatRegistry(os.path.join(tmp_path, "reg.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    r = MessageRouter(reg)
    # no attach_queue call
    surf = _Surface()
    r.subscribe("wa", surf)
    await r.publish(OutboundMessage(session_key="sk", text="hello"))
    assert surf.sent == ["hello"]  # sent directly (no queue)
