"""031: the durable outbound drain HOLDS proactive rows while the owner paused.

Gap this closes: a `message` enqueued for cross-process delivery BEFORE the
owner said stop kept being delivered by ``OutboundDispatcher.drain_once`` after
the pause — the queue had no ``autonomy_control.allows(`` call at all.

Contract: held, never dropped (state stays ``pending``, attempts unchanged, no
dead-letter), and delivered on the next tick after ``resume``. The interactive,
session-bound reply rail (the owner's own chat) is never held.
"""
import asyncio
import time

import pytest

from core.sqlite_util import execute_retry
from core.surfaces.outbound_dispatcher import OutboundDispatcher
from core.surfaces.outbound_queue import OutboundDeliveryQueue


class _Surface:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg.text)
        return type("R", (), {"success": True, "error": None})()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


def _rig(home, *, session_key="direct:telegram:999"):
    q = OutboundDeliveryQueue(str(home / "outbox.db"))
    q.enqueue(idempotency_key="k1", session_key=session_key, surface_id="telegram",
              dest="999", payload="hi", kind="message")
    surface = _Surface()
    return q, surface, OutboundDispatcher(q, lambda sid: surface)


def _row(q):
    return execute_retry(q.db_path, "SELECT * FROM outbound_queue WHERE id=1",
                         fetch="one")


def test_row_enqueued_before_the_pause_is_held_not_dropped(home):
    from core import autonomy_control as ac
    q, surface, disp = _rig(home)
    ac.pause(str(home), via="test")

    assert asyncio.run(disp.drain_once(now=time.time() + 1)) == 0
    assert surface.sent == [], "a held row must never reach the surface"
    row = _row(q)
    assert row["state"] == "pending", "held means pending, not dead"
    assert row["attempts"] == 0, "a hold must not burn a delivery attempt"


def test_held_row_is_delivered_after_resume(home):
    from core import autonomy_control as ac
    q, surface, disp = _rig(home)
    ac.pause(str(home), via="test")
    asyncio.run(disp.drain_once(now=time.time() + 1))
    assert surface.sent == []

    ac.resume(str(home))
    assert asyncio.run(disp.drain_once(now=time.time() + 3600)) == 1
    assert surface.sent == ["hi"]
    assert _row(q)["state"] == "delivered"


def test_social_pause_alone_holds_the_proactive_rail(home):
    from core import autonomy_control as ac
    q, surface, disp = _rig(home)
    ac.pause(str(home), scopes=("social",), via="test")
    assert asyncio.run(disp.drain_once(now=time.time() + 1)) == 0
    assert surface.sent == []


def test_interactive_reply_row_is_never_held(home):
    """R5: the owner's own chat keeps working while paused. A session-bound
    reply row (not the proactive ``direct:`` rail) is delivered."""
    from core import autonomy_control as ac
    q, surface, disp = _rig(home, session_key="telegram:28436760")
    ac.pause(str(home), via="test")
    assert asyncio.run(disp.drain_once(now=time.time() + 1)) == 1
    assert surface.sent == ["hi"]


def test_row_enqueued_after_the_pause_is_delivered(home):
    """An owner-directed proactive send made DURING the pause (the send gate in
    perform_message_send already let it through) must not be silently held —
    the hold covers what was queued BEFORE the stop."""
    from core import autonomy_control as ac
    q, surface, disp = _rig(home)
    res = ac.pause(str(home), via="test")
    execute_retry(q.db_path, "UPDATE outbound_queue SET created_at=? WHERE id=1",
                  ((res.state.since or time.time()) + 10,))
    assert asyncio.run(disp.drain_once(now=time.time() + 1)) == 1
    assert surface.sent == ["hi"]


def test_unpaused_drain_is_unchanged(home):
    q, surface, disp = _rig(home)
    assert asyncio.run(disp.drain_once(now=time.time() + 1)) == 1
    assert surface.sent == ["hi"]
