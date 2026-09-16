"""044 group-presence, round-1 fix report behavioural coverage: end-to-end
``handle_update`` proof that a room never sees owner-only or denied output.

(a) an owner "stop everything" STEER turn inside a room: the gate's own
    confirmation must NOT be posted to the room chat id — it goes to the
    owner's Telegram DM instead.
(b) a silent-DENIED room message: nothing at all is sent to the room.

Reuses the FakeBot/FakeDedup/FakeUD/FakeContainer rig from
test_harness_progress.py rather than re-inventing test doubles.
"""
import types

import pytest

from core.surfaces.dispatcher import RouteKind
from surfaces.telegram.harness import TelegramHarness
from tests.unit.surfaces.telegram.test_harness_progress import (
    FakeBot,
    FakeContainer,
    FakeDedup,
    FakeUD,
    _drain,
)

_ROOM_CHAT_ID = "-100123"
_ROOM_SESSION_KEY = "agent:main:telegram:group:-100123"
_OWNER_UID = "owner_uid"
_OWNER_TG_ID = "777"
_OWNER_DM_ID = "4242"


def _allow_this_room(tmp_path):
    """044 I2: an unlisted room is dropped BEFORE `process_update` now (no paid
    voice download, no user-directory row for a stranger), so a rig that stubs
    `process_update` must allowlist the room it is testing — which is what
    production looks like anyway: a room turn only ever happens in an allowed
    room."""
    import os
    from core.surfaces.group_allowlist import GroupAllowlist
    GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db")).allow(
        "telegram", _ROOM_CHAT_ID)



def _harness(bot):
    return TelegramHarness(
        bot, FakeContainer(), object(),
        webhook_base=None, dedup=FakeDedup(), user_directory=FakeUD(),
    )


def _group_update(uid=1, from_id=_OWNER_TG_ID, text="stop everything"):
    return {"update_id": uid, "message": {
        "chat": {"id": int(_ROOM_CHAT_ID), "type": "group"},
        "from": {"id": int(from_id), "username": "someone"}, "text": text}}


def _result(text, kind, session_key=_ROOM_SESSION_KEY, user_id=_OWNER_UID, silent=False):
    inbound = types.SimpleNamespace(
        text=text, media=[],
        identity=types.SimpleNamespace(user_id=user_id, source=None))
    decision = types.SimpleNamespace(
        kind=kind, session_key=session_key, session_id="s1", command=None, silent=silent)
    return types.SimpleNamespace(inbound=inbound, decision=decision)


class _FakeTaskAgent:
    """Owner-intent gate only needs ensure_session_and_deliver; act_on_inbound is
    never reached in scenario (a) since the gate replies and returns early."""

    async def ensure_session_and_deliver(self, user_id, session_id, text, *, kind="comment",
                                         metadata=None):
        return "delivered"


def _patch(monkeypatch, *, result):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", _OWNER_TG_ID)
    import surfaces.telegram.inbound as inbound_mod
    import surfaces.telegram.surface as surface_mod

    async def fake_process_update(*a, **k):
        return result
    monkeypatch.setattr(inbound_mod, "process_update", fake_process_update)
    monkeypatch.setattr(surface_mod, "chat_id_from_session_key", lambda key: _ROOM_CHAT_ID)


@pytest.mark.asyncio
async def test_owner_stop_everything_in_a_room_goes_to_owner_dm_not_the_room(monkeypatch, tmp_path):
    """Finding 1 (round 1 review): the owner-intent gate's own reply must never
    post into the room — it is owner-only confirmation text."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", _OWNER_UID)
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", _OWNER_DM_ID)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _allow_this_room(tmp_path)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)

    bot = FakeBot()
    result = _result("stop everything", RouteKind.STEER)
    _patch(monkeypatch, result=result)

    harness = _harness(bot)
    harness.task_agent = _FakeTaskAgent()
    await harness.handle_update(_group_update())
    await _drain()

    sent = bot.calls  # (op, chat_id, text, n)
    room_sends = [c for c in sent if c[0] == "send" and c[1] == _ROOM_CHAT_ID]
    owner_sends = [c for c in sent if c[0] == "send" and c[1] == _OWNER_DM_ID]
    assert room_sends == [], f"owner-intent gate reply leaked into the room: {room_sends}"
    assert len(owner_sends) == 1, f"expected exactly one owner-DM send, got {owner_sends}"
    assert "paused" in owner_sends[0][2].lower()


@pytest.mark.asyncio
async def test_silent_denied_room_message_produces_no_output(monkeypatch, tmp_path):
    """Task 3 + finding 2/3 combined proof: a silently-DENIED room message
    produces NOTHING — no bubble, no echo, no breadcrumb, nothing at all."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _allow_this_room(tmp_path)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)

    bot = FakeBot()
    result = _result("anything at all", RouteKind.DENIED, user_id="u_stranger", silent=True)
    _patch(monkeypatch, result=result)

    harness = _harness(bot)
    harness.task_agent = _FakeTaskAgent()
    await harness.handle_update(_group_update(from_id=_OWNER_TG_ID, text="anything at all"))
    await _drain()

    assert bot.sent_texts() == []
