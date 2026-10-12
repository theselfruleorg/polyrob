"""D1 (prod 2026-10-09 19:01): a den-engagement cron answered ``@EmilyWhale``
with ``send_message``. In a cron run that verb reaches ONLY the owner's private
chat, so the room reply landed in the owner's DM, and the result ("Message
sent to user") let the run believe it had posted in the room. A later run then
told the owner "Nothing went to your inbox".
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from core import autonomy_marker
from tools.controller.send_message_route import (
    OWNER_PRIVATE_CHAT_RECEIPT, route_autonomous_send, route_status,
    third_party_address_refusal,
)

DEN_REPLY = ("@EmilyWhale — dev's heads-down working, so DMs get read when he "
             "surfaces. Nothing to chase; if it's on-topic, ask it here and I'll answer.")


@pytest.fixture
def ctrl():
    sid = "sess-den-cron-1"
    autonomy_marker.mark_autonomous(sid)
    yield SimpleNamespace(session_id=sid, orchestrator=SimpleNamespace(user_id="rob"),
                          logger=logging.getLogger("t"))
    autonomy_marker._SESSIONS.pop(sid, None)


def test_autonomous_handle_addressed_text_is_refused_not_sent(ctrl):
    res = third_party_address_refusal(ctrl, DEN_REPLY)
    assert res is not None
    assert "NOT SENT" in res.extracted_content
    assert "message(surface=" in res.extracted_content
    assert "@EmilyWhale" in res.extracted_content


def test_owner_report_that_mentions_a_handle_later_is_not_refused(ctrl):
    assert third_party_address_refusal(
        ctrl, "Den: muted @spam for promo. No other action.") is None


def test_interactive_session_is_never_refused():
    c = SimpleNamespace(session_id="interactive-1", orchestrator=SimpleNamespace())
    assert third_party_address_refusal(c, DEN_REPLY) is None


def test_room_bound_autonomous_session_is_never_refused(ctrl):
    ctrl.orchestrator._public_session = True
    assert third_party_address_refusal(ctrl, DEN_REPLY) is None


def test_sent_receipt_names_the_owner_private_chat():
    out = route_status("sent")
    assert out == OWNER_PRIVATE_CHAT_RECEIPT
    assert "private chat" in out and "never posts in a room" in out
    assert route_status(None) is None
    assert "NOT delivered" in route_status("capped")


def test_route_autonomous_send_delegates_and_fails_open(ctrl, monkeypatch):
    import core.surfaces.user_delivery as ud

    async def _ok(orch, sid, text):
        return "sent"
    monkeypatch.setattr(ud, "maybe_deliver_autonomous_send", _ok)
    assert asyncio.run(route_autonomous_send(ctrl, "hi")) == "sent"

    async def _boom(orch, sid, text):
        raise RuntimeError("x")
    monkeypatch.setattr(ud, "maybe_deliver_autonomous_send", _boom)
    assert asyncio.run(route_autonomous_send(ctrl, "hi")) is None


def test_owner_thread_line_says_it_went_to_the_private_chat():
    from core.surfaces.owner_thread import render_line
    line = render_line({"direction": "out", "ts": 0, "via": "telegram",
                        "source": "agent_send", "mid": "40156", "body": DEN_REPLY})
    assert "PRIVATE chat" in line and "via telegram" in line
    assert "[msg 40156]" in line
