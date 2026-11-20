"""110 W3: ReplyTarget — a proactive send lands in a thread / as a reply, live and queued."""
import os
import sqlite3

import pytest

from core.surfaces.envelopes import Action, ReplyTarget, SendResult
from core.surfaces.message_router import MessageRouter
from core.surfaces.outbound_dispatcher import OutboundDispatcher
from core.surfaces.outbound_queue import OutboundDeliveryQueue
from core.surfaces.session_chat_registry import (
    SessionChatRegistry, chat_id_from_session_key, direct_session_key)


class _Surface:
    surface_id = "telegram"

    def __init__(self):
        self.msgs = []

    async def send(self, msg):
        self.msgs.append(msg)
        return SendResult(success=True)

    async def stream(self, msg):
        ...


def _router(tmp_path, queue=False):
    r = MessageRouter(SessionChatRegistry(os.path.join(tmp_path, "reg.db")))
    q = None
    if queue:
        q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
        r.attach_queue(q)
    return r, q


def test_target_validation():
    assert ReplyTarget("telegram", "-100", thread_id="7").threaded
    assert not ReplyTarget("telegram", "-100").threaded
    for bad in ("7:admin:1", "a b", "x" * 200, "7:thread:8"):
        with pytest.raises(ValueError):
            ReplyTarget("telegram", "-100", thread_id=bad)
    with pytest.raises(ValueError):
        ReplyTarget("telegram", "")
    with pytest.raises(ValueError):
        ReplyTarget("telegram", "1", reply_to="a\nb")


def test_direct_key_roundtrips_the_chat():
    assert direct_session_key("telegram", "-100") == "direct:telegram:-100"
    k = direct_session_key("telegram", "-100", "7")
    assert k == "direct:telegram:-100:thread:7"
    assert chat_id_from_session_key(k) == "-100"
    assert chat_id_from_session_key("direct:telegram:-100") == "-100"


@pytest.mark.asyncio
async def test_forum_topic_send_lands_in_the_topic_with_the_anchor(tmp_path):
    r, _ = _router(tmp_path)
    s = _Surface()
    r.subscribe("telegram", s)
    rc = await r.send_message_receipt(
        "-100", "hi", "telegram", target=ReplyTarget("telegram", "-100", "7", "55"))
    assert rc.status == "sent"
    m = s.msgs[0]
    assert m.session_key == "direct:telegram:-100:thread:7" and m.reply_to == "55"


@pytest.mark.asyncio
async def test_no_target_is_byte_identical(tmp_path):
    r, _ = _router(tmp_path)
    s = _Surface()
    r.subscribe("telegram", s)
    await r.send_message_receipt("-100", "hi", "telegram")
    m = s.msgs[0]
    assert m.session_key == "direct:telegram:-100" and m.reply_to is None and m.actions == []


@pytest.mark.asyncio
async def test_target_for_another_chat_is_refused(tmp_path):
    r, _ = _router(tmp_path)
    s = _Surface()
    r.subscribe("telegram", s)
    rc = await r.send_message_receipt(
        "-100", "hi", "telegram", target=ReplyTarget("telegram", "-200", "7"))
    assert rc.status == "failed" and not s.msgs


@pytest.mark.asyncio
async def test_queued_send_keeps_thread_reply_to_and_actions(tmp_path):
    r, q = _router(tmp_path, queue=True)  # no local surface -> cross-process queue
    act = Action(label="Status", command="/status")
    rc = await r.send_message_receipt(
        "-100", "hi", "telegram", target=ReplyTarget("telegram", "-100", "7", "55"),
        actions=[act])
    assert rc.status == "queued"
    s = _Surface()
    d = OutboundDispatcher(q, lambda sid: s, rate_per_sec=1000, burst=1000)
    assert await d.drain_once(now=1e12) == 1
    m = s.msgs[0]
    assert (m.session_key, m.reply_to, m.actions) == (
        "direct:telegram:-100:thread:7", "55", [act])


@pytest.mark.asyncio
async def test_publish_queue_row_keeps_reply_to(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    r, q = _router(tmp_path, queue=True)
    r._registry.bind("sk", "sid", "u1", "telegram", "123")
    r.subscribe("telegram", _Surface())
    from core.surfaces.envelopes import OutboundMessage
    await r.publish(OutboundMessage(session_key="sk", text="hello", reply_to="9"))
    row = q.claim_due(1e12)[0]
    assert row["reply_to"] == "9"


def test_old_queue_db_gains_columns_and_keeps_rows(tmp_path):
    db = os.path.join(tmp_path, "o.db")
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE outbound_queue (id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT UNIQUE, session_key TEXT NOT NULL, surface_id TEXT NOT NULL,
        dest TEXT, payload TEXT NOT NULL, media TEXT, kind TEXT DEFAULT 'agent_text',
        state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
        next_attempt_at REAL DEFAULT 0, last_error TEXT, created_at REAL, updated_at REAL,
        claim_token TEXT)""")
    c.execute("INSERT INTO outbound_queue (idempotency_key,session_key,surface_id,dest,payload)"
              " VALUES ('k','direct:telegram:1','telegram','1','old')")
    c.commit()
    c.close()
    q = OutboundDeliveryQueue(db)
    row = q.claim_due(1e12)[0]
    assert row["payload"] == "old" and row["reply_to"] is None and row["actions"] is None


@pytest.mark.asyncio
async def test_message_tool_reply_to_and_thread_reach_the_router():
    from tools.controller.message_send import perform_message_send
    from core.surfaces.message_router import SendReceipt

    class R:
        calls = []

        async def send_message_receipt(self, chat, text, surface_id=None, media=None, **kw):
            R.calls.append((chat, kw))
            return SendReceipt("sent")

    out = await perform_message_send(
        router=R(), allowlist=None, owner_targets={"telegram": "-100"}, user_id="u",
        surface="telegram", target="owner", text="hi", reply_to="55", thread_id="7")
    assert out["success"], out
    t = R.calls[0][1]["target"]
    assert (t.chat_id, t.thread_id, t.reply_to) == ("-100", "7", "55")
    R.calls.clear()
    await perform_message_send(router=R(), allowlist=None, owner_targets={"telegram": "-100"},
                               user_id="u", surface="telegram", target="owner", text="hi")
    assert R.calls[0][1] == {}  # no target: the router is called as before
    bad = await perform_message_send(
        router=R(), allowlist=None, owner_targets={"telegram": "-100"}, user_id="u",
        surface="telegram", target="owner", text="hi", thread_id="7:admin:1")
    assert not bad["success"] and "reply target" in bad["error"]
    bad = await perform_message_send(
        router=R(), allowlist=None, owner_targets={"telegram": "-100"}, user_id="u",
        surface="telegram", target="owner", text="hi", thread_id="general")
    assert not bad["success"] and "numeric" in bad["error"]


@pytest.mark.asyncio
async def test_deliver_user_message_threads_through_the_router(tmp_path, monkeypatch):
    from core.surfaces import user_delivery as ud

    seen = {}

    class Router:
        async def send_message_ex(self, chat, text, surface_id="telegram", **kw):
            seen.update(chat=chat, surface=surface_id, **kw)
            return "sent"

    class Sink:  # the bot sink must NOT be used for a threaded send
        async def send_message(self, *a, **k):
            seen["sink"] = True
            return True

    class C:
        def get_service(self, n):
            return {"telegram_sink": Sink(), "message_router": Router()}.get(n)

    tgt = ReplyTarget("telegram", "-100", "7", "55")
    out = await ud.deliver_user_message(
        C(), "u1", "hello", source="agent", recipient_override="-100",
        recipient_surface="telegram", reply_target=tgt, event_log=None)
    assert out == "sent", out
    assert seen["target"] is tgt and "sink" not in seen and seen["chat"] == "-100"


def test_held_args_round_trip_the_target():
    from core.surfaces.user_delivery import _held_args, _held_reply_target
    t = ReplyTarget("telegram", "-100", "7", "55")
    assert _held_reply_target(_held_args(None, None, None, None, None, None, t)["reply_target"]) == t
    assert "reply_target" not in _held_args(None, None, None, None, None, None)
    assert _held_reply_target({"surface_id": "x"}) is None


def test_held_args_round_trip_the_buttons():
    """A notice held by quiet hours keeps its next-step buttons when it is released."""
    from core.surfaces.user_delivery import _held_args, _held_buttons
    b = [Action(label="Show", command="/status"), Action("Retry", "/help", "primary")]
    held = _held_args(None, None, None, None, None, None, None, b)["buttons"]
    assert _held_buttons(held) == b
    assert "buttons" not in _held_args(None, None, None, None, None, None)
    assert _held_buttons([{"label": "x"}]) is None
