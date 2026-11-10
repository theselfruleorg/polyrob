"""F-03 (113 W7): the owner's verb typed in a room, off Telegram.

The reply is owner-only output, so it must never enter the room — and before
this it went nowhere: ``owner_only_reply_target`` knows only the Telegram DM,
so the owner read silence. Now the shared tail answers in his DM on the SAME
app (``core.surfaces.owner_address``) and the room reads one line that says so.
"""
import asyncio

import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces._shared import ANSWERED_IN_DM, DM_UNREACHABLE

OWNER = "u_owner_fixture"


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", OWNER)
    monkeypatch.setenv("OWNER_SLACK_ID", "U0OWNER")
    monkeypatch.setenv("OWNER_DISCORD_ID", "900")
    monkeypatch.setenv("OWNER_SIGNAL_ID", "+15550000001")


def _wire(monkeypatch, kind=RouteKind.COMMAND, replies=("secret status",)):
    async def _route(container, inbound):
        src = inbound.identity.source
        return RouteDecision(kind, f"agent:main:{src.surface_id}:{src.chat_type}:{src.chat_id}",
                             command="/status")

    async def _act(task_agent, result, *, deliver, **kw):
        for r in replies[:-1]:
            await deliver(r)
        return replies[-1]
    monkeypatch.setattr("core.surfaces.dispatcher.route_inbound", _route)
    monkeypatch.setattr("surfaces._actor.act_on_inbound", _act)


def _inbound(surface="slack", chat_type="group", user=OWNER):
    return InboundMessage(text="/status", identity=Identity(
        user_id=user, source=SessionSource(surface, "C1", chat_type), raw_user_id="R1"))


def _run(inbound, *, dm=True, dm_fails=False):
    from surfaces._shared import route_and_act
    room, dms = [], []

    async def deliver(text):
        room.append(text)

    async def deliver_dm(address, text):
        if dm_fails:
            raise RuntimeError("cannot open DM")
        dms.append((address, text))
    asyncio.run(route_and_act(None, None, inbound, deliver,
                              deliver_dm=deliver_dm if dm else None))
    return room, dms


def test_owner_room_verb_is_answered_in_his_dm(monkeypatch):
    _wire(monkeypatch)
    room, dms = _run(_inbound())
    assert dms == [("U0OWNER", "secret status")]
    assert room == [ANSWERED_IN_DM]


def test_the_room_note_goes_out_once_per_line(monkeypatch):
    _wire(monkeypatch, replies=("⏳ started", "secret result"))
    room, dms = _run(_inbound())
    assert [t for _a, t in dms] == ["⏳ started", "secret result"]
    assert room == [ANSWERED_IN_DM]


def test_no_reachable_dm_is_said_and_the_answer_stays_out(monkeypatch):
    _wire(monkeypatch)
    room, dms = _run(_inbound(), dm_fails=True)
    assert dms == [] and room == [DM_UNREACHABLE]
    monkeypatch.delenv("OWNER_SLACK_ID")
    room, dms = _run(_inbound())
    assert dms == [] and room == [DM_UNREACHABLE]


def test_a_non_owner_room_verb_stays_silent(monkeypatch):
    _wire(monkeypatch)
    room, dms = _run(_inbound(user="u_slack_member"))
    assert room == [] and dms == []


def test_a_room_turn_is_not_redirected(monkeypatch):
    _wire(monkeypatch, kind=RouteKind.STEER)
    room, dms = _run(_inbound())
    assert room == [] and dms == []


def test_a_transport_without_a_dm_sender_keeps_the_old_outcome(monkeypatch):
    _wire(monkeypatch)
    room, dms = _run(_inbound(), dm=False)
    assert room == [] and dms == []


def test_a_dm_verb_is_unchanged(monkeypatch):
    _wire(monkeypatch)
    room, dms = _run(_inbound(chat_type="dm"))
    assert room == ["secret status"] and dms == []


# --- each transport's DM sender ---------------------------------------------

def test_slack_dm_is_the_user_id():
    from surfaces.slack.harness import SlackHarness
    sent = []

    class _C:
        async def send_message(self, channel, text, thread_ts=None):
            sent.append((channel, text, thread_ts))
    h = SlackHarness(None, None, _C(), socket=None, dedup=None)
    asyncio.run(h._dm_sender()("U0OWNER", "hi"))
    assert sent == [("U0OWNER", "hi", None)]


def test_discord_dm_opens_the_dm_channel_first():
    from surfaces.discord.harness import DiscordHarness
    sent = []

    class _C:
        async def create_dm(self, user_id):
            return f"dm-{user_id}"

        async def send_message(self, channel, text, reply_to=None):
            sent.append((channel, text))
    h = DiscordHarness(None, None, _C(), gateway=None, dedup=None)
    asyncio.run(h._dm_sender()("900", "hi"))
    assert sent == [("dm-900", "hi")]


def test_signal_dm_is_the_number_and_a_long_reply_is_split():
    from surfaces.signal.harness import SignalHarness
    sent = []

    class _C:
        account = ""

        async def send(self, recipient, text):
            sent.append((recipient, text))
    h = SignalHarness(None, None, _C(), stream=None, dedup=None)
    asyncio.run(h._dm_sender()("+15550000001", "x" * 5000))
    assert len(sent) > 1 and all(r == "+15550000001" for r, _t in sent)


def test_a_transport_that_did_not_opt_in_has_no_dm_sender():
    from surfaces._shared import BaseHarness
    assert BaseHarness(None, None, None)._dm_sender() is None
