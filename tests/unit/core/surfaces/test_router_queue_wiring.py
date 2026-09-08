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


@pytest.mark.asyncio
async def test_queue_carries_media_end_to_end(tmp_path, monkeypatch):
    """030 L4: enqueue must persist OutboundMessage.media and the dispatcher must
    reconstruct it — enabling the queue used to silently drop every photo,
    document and invoice card."""
    import time as _t

    from core.surfaces.outbound_dispatcher import OutboundDispatcher

    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    reg = SessionChatRegistry(os.path.join(tmp_path, "reg.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    r = MessageRouter(reg)
    r.attach_queue(q)

    class _MediaSurface(_Surface):
        def __init__(self):
            super().__init__()
            self.media = []

        async def send(self, msg):
            self.sent.append(msg.text)
            self.media.append(msg.media)
            return SendResult(success=True)

    surf = _MediaSurface()
    r.subscribe("wa", surf)
    media = [{"kind": "image", "path": "/tmp/card.png", "caption": "invoice #7"}]
    await r.publish(OutboundMessage(session_key="sk", text="your invoice", media=media))
    assert q.counts()["pending"] == 1 and surf.sent == []

    d = OutboundDispatcher(q, lambda sid: surf, rate_per_sec=1000, burst=1000)
    delivered = await d.drain_once(_t.time())
    assert delivered == 1
    assert surf.sent == ["your invoice"]
    assert surf.media == [media]


def test_media_column_added_to_a_preexisting_queue_db(tmp_path):
    """The media column arrives via an additive ALTER on open — an existing queue
    DB from an older version must keep working."""
    import sqlite3

    db = os.path.join(tmp_path, "legacy.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE outbound_queue (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             idempotency_key TEXT UNIQUE, session_key TEXT NOT NULL,
             surface_id TEXT NOT NULL, dest TEXT, payload TEXT NOT NULL,
             kind TEXT DEFAULT 'agent_text', state TEXT DEFAULT 'pending',
             attempts INTEGER DEFAULT 0, next_attempt_at REAL DEFAULT 0,
             last_error TEXT, created_at REAL DEFAULT (strftime('%s','now')),
             updated_at REAL DEFAULT (strftime('%s','now')))""")
    conn.commit()
    conn.close()
    q = OutboundDeliveryQueue(db)
    assert q.enqueue(idempotency_key="k1", session_key="sk", surface_id="wa",
                     dest="1", payload="t", media=[{"kind": "image", "path": "/x.png"}])
    row = q.claim_due(9999999999.0)[0]
    assert row["media"]
