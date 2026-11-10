"""113 W4 item 6: seats on the rows that need them.

F-07: `/ban /unban /unmute` act through the Telegram moderator. The row names
the capability (`Verb.needs = "moderation"`), the surface catalog row names who
has it, and `runs_on` reads both — so a chat surface without it neither lists
the verb nor runs it. `/mute` silences a room (the agent's own presence) on
every seat. I-5 / I-1: the help lines say how a chat app steers and where a
skill is installed.
"""
import asyncio

import pytest

from core import verbs
from core.surfaces import catalog

MEMBER_VERBS = ("/ban", "/unban", "/unmute")


def test_only_telegram_has_moderation():
    assert catalog.capability_ids("moderation") == ("telegram",)


@pytest.mark.parametrize("name", MEMBER_VERBS)
def test_a_member_verb_runs_where_the_moderator_is(name):
    row = verbs.verb_for(name)
    assert row.needs == "moderation"
    assert row.runs_on("telegram") and row.runs_on("repl") and row.runs_on("console")
    for seat in ("discord", "slack", "signal", "whatsapp"):
        assert not row.runs_on(seat), f"{name} claims {seat}"
        assert name not in {v.name for v in verbs.seat_verbs(seat)}


def test_room_silence_stays_on_every_seat():
    assert verbs.verb_for("/mute").runs_on("discord")


def test_another_chat_surface_is_told_where_it_runs():
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    from surfaces.telegram.admin_verbs import pre_gate_reply
    source = SessionSource(surface_id="discord", chat_id="1", chat_type="group")
    inbound = InboundMessage(text="/ban 1h", identity=Identity(user_id="u1", source=source))
    result = type("R", (), {"inbound": inbound})()
    out = asyncio.run(pre_gate_reply(None, result, "/ban"))
    assert "It runs in Telegram or the terminal or the console, not here." in out


def test_help_lines_name_where_it_runs():
    assert "chat app" in verbs.verb_for("/steer").help
    assert "terminal" in verbs.verb_for("/skills").help
