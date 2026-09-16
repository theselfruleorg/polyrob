"""044 T17: the room's own policy decides what the room is CALLED and how much
of it the model is shown.

The Telegram title is attacker-authorable — any room admin can rename the room —
so an owner who has named the room must win over it.
"""
import time
import types

import pytest

from core.surfaces.chat_policy import set as set_pol
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.group_ledger import GroupLedger, LedgerRow
from surfaces.telegram.room_turn import chat_title, room_turn_text

_OWNER = "rob"
_CHAT = "-100"


@pytest.fixture(autouse=True)
def _owner_env(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", _OWNER)
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "polyrob")


def _container(tmp_path, ledger=None):
    svc = {"group_ledger": ledger} if ledger is not None else {}
    return types.SimpleNamespace(
        config=types.SimpleNamespace(data_dir=str(tmp_path)),
        get_service=lambda n: svc.get(n))


def _inbound(text="hi", *, title="Telegram's Own Title", message_id=9):
    src = SessionSource(surface_id="telegram", chat_id=_CHAT, chat_type="supergroup")
    ident = Identity(user_id="u1", source=src, raw_user_id="u1")
    raw = {"message": {"message_id": message_id, "chat": {"id": _CHAT, "title": title},
                       "from": {"id": 1, "username": "someone"}}}
    return InboundMessage(text=text, identity=ident, raw=raw, mentions_bot=True)


def test_the_owners_name_beats_the_telegram_title(tmp_path):
    ok, msg = set_pol(tmp_path, _OWNER, "telegram", _CHAT, "chat.name", "The Public Den")
    assert ok, msg
    assert chat_title(_container(tmp_path), _inbound()) == "The Public Den"


def test_without_a_name_the_telegram_title_is_used(tmp_path):
    assert chat_title(_container(tmp_path), _inbound()) == "Telegram's Own Title"


def test_without_either_the_chat_id_is_used(tmp_path):
    assert chat_title(_container(tmp_path), _inbound(title="")) == _CHAT


def test_context_lines_bounds_what_the_model_is_shown(tmp_path):
    ledger = GroupLedger(str(tmp_path / "surfaces.db"))
    for i in range(6):
        ledger.append(LedgerRow(
            surface="telegram", chat_id=_CHAT, thread_id=None, message_id=str(i),
            ts=time.time() - 10 + i, sender_id=f"u{i}", sender_name=f"@m{i}",
            sender_is_bot=False,
            role_at_write="member", kind="text", text=f"line {i}",
            reply_to_message_id=None, mentions_bot=False, media_path=None))

    agent = types.SimpleNamespace(container=_container(tmp_path, ledger))
    turn, role = room_turn_text(agent, types.SimpleNamespace(inbound=_inbound()))
    assert role == "member"
    assert "line 0" in turn.context and "line 5" in turn.context

    ok, msg = set_pol(tmp_path, _OWNER, "telegram", _CHAT, "chat.context_lines", 2)
    assert ok, msg
    turn2, _ = room_turn_text(agent, types.SimpleNamespace(inbound=_inbound()))
    # The ledger tail is newest-first-bounded, so a 2-line budget shows the two
    # most recent lines and drops the oldest.
    assert "line 5" in turn2.context and "line 0" not in turn2.context


def test_a_zero_context_budget_answers_with_no_room_context(tmp_path):
    ledger = GroupLedger(str(tmp_path / "surfaces.db"))
    ledger.append(LedgerRow(
        surface="telegram", chat_id=_CHAT, thread_id=None, message_id="1",
        ts=time.time() - 1, sender_id="u1", sender_name="@m1", sender_is_bot=False,
        role_at_write="member",
        kind="text", text="earlier chatter", reply_to_message_id=None,
        mentions_bot=False, media_path=None))
    ok, msg = set_pol(tmp_path, _OWNER, "telegram", _CHAT, "chat.context_lines", 0)
    assert ok, msg
    agent = types.SimpleNamespace(container=_container(tmp_path, ledger))
    turn, _ = room_turn_text(agent, types.SimpleNamespace(inbound=_inbound()))
    assert turn.context == ""
    assert "hi" in turn.addressed   # the addressed line always survives
