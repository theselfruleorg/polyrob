"""The room log must hold BOTH sides of the conversation.

Until 2026-09-16 `record_inbound_to_ledger` was the ledger's only writer, so
`group_ledger` held every human line and not one of the agent's own. A room
session that was evicted or restarted came back unable to see a single thing it
had said; `/groups tail` showed the owner half a conversation; and a service
goal could re-answer a line it had already answered.
"""
import time
from types import SimpleNamespace

import pytest

from core.surfaces.group_ledger import GroupLedger, LedgerRow
from core.surfaces.ledger_ingest import record_outbound_to_ledger

ROOM_KEY = "agent:main:telegram:group:-100123"

#: ⚠️ `GroupLedger.prune` is WALL-CLOCK (14-day retention) and runs on every
#: append, so a fixture timestamp near the epoch is deleted the instant it is
#: written. Anchor every row to now.
NOW = time.time()


def _ledger(tmp_path):
    return GroupLedger(str(tmp_path / "surfaces.db"))


def _human(mid, text, ts):
    return LedgerRow(
        surface="telegram", chat_id="-100123", thread_id=None, message_id=str(mid),
        ts=ts, sender_id="7", sender_name="@alexey", sender_is_bot=False,
        role_at_write="member", kind="text", text=text,
        reply_to_message_id=None, mentions_bot=True)


def test_the_agents_reply_is_recorded_and_readable(tmp_path):
    lg = _ledger(tmp_path)
    lg.append(_human(1, "Go boy, do it!", NOW))
    assert record_outbound_to_ledger(
        lg, surface="telegram", chat_id="-100123", thread_id=None,
        text="Alexey — claiming the fees first.", ts=NOW + 1) is True
    rows = lg.tail("telegram", "-100123", limit=10)
    assert [(r.sender_is_bot, r.text) for r in rows] == [
        (False, "Go boy, do it!"),
        (True, "Alexey — claiming the fees first."),
    ]


def test_the_agents_own_lines_survive_the_answered_burn(tmp_path):
    """`unanswered_only` asks "has anyone answered this line?" — a question only
    a HUMAN line can be asked. Burning the agent's own rows would empty the log
    of its half again, one turn later."""
    lg = _ledger(tmp_path)
    lg.append(_human(1, "Go boy, do it!", NOW))
    record_outbound_to_ledger(lg, surface="telegram", chat_id="-100123",
                              thread_id=None, text="On it.", ts=NOW + 1)
    shown = [r.message_id for r in lg.tail("telegram", "-100123", limit=10)]
    lg.mark_answered("telegram", "-100123", shown, "sess-1")   # what a turn does

    left = lg.tail("telegram", "-100123", limit=10, unanswered_only=True)
    assert [r.text for r in left] == ["On it."], (
        "the agent's own line was burned with the human lines — the next turn "
        "sees a room in which it has never spoken")


def test_a_thread_scoped_reply_lands_on_its_own_thread(tmp_path):
    """`tail` filters `AND thread_id=?`. Recording the agent on the wrong thread
    makes its line invisible to the very context block that needs it."""
    lg = _ledger(tmp_path)
    record_outbound_to_ledger(lg, surface="telegram", chat_id="-100123",
                              thread_id="7", text="threaded answer", ts=NOW + 1)
    assert [r.text for r in lg.tail("telegram", "-100123", thread_id="7")] == [
        "threaded answer"]
    assert lg.tail("telegram", "-100123", thread_id="9") == []


def test_an_empty_reply_is_never_written(tmp_path):
    lg = _ledger(tmp_path)
    assert record_outbound_to_ledger(lg, surface="telegram", chat_id="-100123",
                                     thread_id=None, text="   ", ts=NOW) is False
    assert lg.count("telegram", "-100123") == 0


def test_a_retried_send_updates_rather_than_duplicates(tmp_path):
    """The synthetic message_id hashes the body, so the durable queue retrying
    the same payload cannot double the room's log."""
    lg = _ledger(tmp_path)
    for _ in range(3):
        record_outbound_to_ledger(lg, surface="telegram", chat_id="-100123",
                                  thread_id=None, text="same body", ts=NOW + 1)
    assert lg.count("telegram", "-100123") == 1


def test_a_ledger_fault_never_costs_the_message(tmp_path):
    """Bookkeeping runs AFTER delivery. A fault must be reported, not raised."""
    class _Broken:
        def append(self, row):
            raise RuntimeError("disk full")
    assert record_outbound_to_ledger(_Broken(), surface="telegram",
                                     chat_id="-1", thread_id=None,
                                     text="delivered already", ts=NOW) is False


# --- the router half: only a DELIVERED room reply is recorded ----------------

class _Recorder:
    def __init__(self):
        self.rows = []

    def append(self, row):
        self.rows.append(row)

    def prune(self, *a, **k):
        pass


def _router(recorder):
    from core.surfaces.message_router import MessageRouter
    from core.surfaces.session_chat_registry import SessionChatRegistry

    class _Reg(SessionChatRegistry):
        def __init__(self):
            pass

        def resolve(self, key):
            return {"surface_id": "telegram", "chat_id": "-100123"}

    class _Surface:
        capabilities = SimpleNamespace(media_out=False)

        async def send(self, msg):
            return SimpleNamespace(success=True, error=None)

        async def stream(self, msg):
            return None

    r = MessageRouter(_Reg())
    r._surfaces["telegram"] = _Surface()
    r.attach_room_ledger(recorder)
    return r


@pytest.mark.asyncio
async def test_router_records_a_delivered_room_reply(monkeypatch):
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "false")
    from core.surfaces.envelopes import OutboundMessage
    rec = _Recorder()
    ok = await _router(rec).publish(OutboundMessage(
        session_key=ROOM_KEY, text="Alexey — on it.", partial=False))
    assert ok is True
    assert [(r.sender_is_bot, r.text) for r in rec.rows] == [(True, "Alexey — on it.")]


@pytest.mark.asyncio
async def test_router_does_not_record_a_suppressed_silent(monkeypatch):
    """`[SILENT]` costs no message, so it is not something the room heard.
    Recording it would put the sentinel itself into the room's context."""
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "false")
    from core.surfaces.envelopes import OutboundMessage
    rec = _Recorder()
    ok = await _router(rec).publish(OutboundMessage(
        session_key=ROOM_KEY, text="[SILENT]", partial=False))
    assert ok is False
    assert rec.rows == []


@pytest.mark.asyncio
async def test_router_does_not_record_a_dm(monkeypatch):
    """A DM has no room ledger. A DM reply written into one would leak the
    owner's private conversation into a store built for public rooms."""
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "false")
    from core.surfaces.envelopes import OutboundMessage
    rec = _Recorder()
    await _router(rec).publish(OutboundMessage(
        session_key="agent:main:telegram:dm:555:u_abc", text="private", partial=False))
    assert rec.rows == []
