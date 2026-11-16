"""A room the bot was kicked from comes back when the bot is added back.

Prod 2026-11-15: a send failed with "bot was kicked", the room row went ``left``
and the dead-target latch was set. The owner re-added the bot (Telegram showed
it as administrator), but the poller never asked for ``my_chat_member``, so
nothing restored the row: every room tool refused the room for 14 hours, with
the false line "is not an allowlisted room".
"""
from types import SimpleNamespace

import pytest

from core.surfaces.dead_targets import DeadTargetStore
from core.surfaces.group_allowlist import GroupAllowlist
from surfaces.telegram import room_membership

CHAT = "-1001000000002"
NOTES = []  # the owner notes the fixture captures


def _update(status, *, chat_type="supergroup", is_member=None):
    new = {"status": status, "user": {"id": 1, "is_bot": True}}
    if is_member is not None:
        new["is_member"] = is_member
    return {"update_id": 7, "my_chat_member": {
        "chat": {"id": int(CHAT), "type": chat_type, "title": "The Lobby"},
        "from": {"id": 100000001}, "date": 0,
        "old_chat_member": {"status": "left"}, "new_chat_member": new}}


class _Container:
    def __init__(self, home, dead=None):
        self.config = SimpleNamespace(data_dir=str(home))
        self._svc = {"dead_targets": dead} if dead else {}

    def get_service(self, name):
        return self._svc.get(name)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    NOTES.clear()

    async def _deliver(container, uid, text):
        NOTES.append(text)

    import core.surfaces.user_delivery as ud
    import core.instance as inst
    monkeypatch.setattr(ud, "deliver_user_message", _deliver)
    monkeypatch.setattr(inst, "resolve_owner_user_id", lambda: "owner")
    return tmp_path


def _al(home):
    return GroupAllowlist(str(home / "group_allowlist.db"))


def _status(home):
    return {r["chat_id"]: r["status"] for r in _al(home).list_all()}


# --- parsing -------------------------------------------------------------------

@pytest.mark.parametrize("status,is_member,want", [
    ("member", None, True), ("administrator", None, True), ("creator", None, True),
    ("restricted", True, True), ("restricted", False, False),
    ("left", None, False), ("kicked", None, False),
])
def test_membership_of_reads_join_and_departure(status, is_member, want):
    assert room_membership.membership_of(_update(status, is_member=is_member)) == (
        CHAT, "The Lobby", want)


def test_a_private_chat_or_another_update_is_not_a_room_change():
    assert room_membership.membership_of(_update("kicked", chat_type="private")) is None
    assert room_membership.membership_of({"message": {"text": "hi"}}) is None


# --- the rejoin ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_rejoin_restores_a_left_room_and_lifts_the_dead_latch(home):
    _al(home).allow("telegram", CHAT, "The Lobby")
    _al(home).mark_left("telegram", CHAT)
    dead = DeadTargetStore(str(home / "dead_targets.db"))
    dead.mark("telegram", CHAT, "kicked")
    harness = SimpleNamespace(container=_Container(home, dead))

    assert await room_membership.absorb(harness, _update("administrator")) is True
    assert _al(home).is_allowed("telegram", CHAT)
    assert not dead.is_dead("telegram", CHAT)
    assert len(NOTES) == 1 and "active again" in NOTES[0]


@pytest.mark.asyncio
async def test_an_add_never_allows_a_revoked_or_unknown_room(home):
    _al(home).allow("telegram", CHAT, "The Lobby")
    _al(home).revoke("telegram", CHAT)
    c = _Container(home)
    await room_membership.on_change(c, CHAT, "The Lobby", True)
    await room_membership.on_change(c, "-1009", "Elsewhere", True)
    assert _status(home) == {CHAT: "revoked"}
    assert NOTES == []


@pytest.mark.asyncio
async def test_a_kick_marks_the_room_left_at_once(home):
    _al(home).allow("telegram", CHAT, "The Lobby")
    harness = SimpleNamespace(container=_Container(home))
    assert await room_membership.absorb(harness, _update("kicked")) is True
    assert _status(home) == {CHAT: "left"}
    assert "removed" in NOTES[0]


@pytest.mark.asyncio
async def test_a_rights_change_moves_nothing(home):
    _al(home).allow("telegram", CHAT, "The Lobby")
    assert await room_membership.on_change(_Container(home), CHAT, "x", None) is False
    assert _status(home) == {CHAT: "active"} and NOTES == []


# --- the wiring --------------------------------------------------------------------

def test_the_poller_asks_for_my_chat_member():
    from surfaces.telegram.harness import _ALLOWED_UPDATES
    assert "my_chat_member" in _ALLOWED_UPDATES


def test_a_membership_update_keeps_its_room_lane():
    from surfaces.telegram.poll_batch import _chat_key
    assert _chat_key(_update("member")) == CHAT


def test_an_owner_re_allow_also_lifts_the_dead_latch(home):
    from core.surfaces import group_admin
    _al(home).allow("telegram", CHAT, "The Lobby")
    _al(home).mark_left("telegram", CHAT)
    dead = DeadTargetStore(str(home / "dead_targets.db"))
    dead.mark("telegram", CHAT, "kicked")
    group_admin.allow_here(_Container(home, dead), "telegram", CHAT, "", owner_uid="owner")
    assert _al(home).is_allowed("telegram", CHAT)
    assert not dead.is_dead("telegram", CHAT)


# --- the refusal names the truth ---------------------------------------------------

def test_left_room_hint_says_removed_not_unallowlisted():
    from core.surfaces.room_keys import left_room_hint
    rows = [{"surface": "telegram", "chat_id": CHAT, "status": "left"}]
    hint = left_room_hint(rows, CHAT, lambda r: r["chat_id"] == CHAT)
    assert "REMOVED" in hint and "/groups allow here" in hint
    assert "not an allowlisted room" not in hint
    assert left_room_hint(rows, "-1009", lambda r: False) == ""
