"""/help on a chat app lists only what that app runs: a room verb (/groups, /mute,
/ban …) only where a room transport is registered — the registry answers, never a
list of surface names."""
import pytest

from core.surfaces import command_catalog as cc
from core.surfaces import room_transport as rt


@pytest.fixture
def telegram_only(monkeypatch):
    monkeypatch.setattr(rt, "_FACTORIES", {"telegram": lambda agent: rt.NullRoomTransport()})
    monkeypatch.setattr(cc, "_BODY", {})


def _names(body):
    return {ln.split(" ")[0] for ln in body.splitlines() if ln.startswith("/")}


def test_a_chat_app_without_rooms_lists_no_room_verb(telegram_only):
    slack = _names(cc.help_body("slack"))
    assert not slack & rt.ROOM_VERBS
    assert "/status" in slack
    telegram = _names(cc.help_body("telegram"))
    assert {"/groups", "/mute"} <= telegram


def test_the_menu_and_groups_follow_the_seat(telegram_only):
    from surfaces.telegram import help_menu
    slack = {v.name for _g, vs in help_menu.groups("slack") for v in vs}
    tg = {v.name for _g, vs in help_menu.groups("telegram") for v in vs}
    assert "/groups" in tg and "/groups" not in slack
    reply = help_menu.group_reply("set up", "u1", "slack")
    assert "/mute" not in reply.text


def test_help_for_a_room_verb_names_the_app_with_rooms(telegram_only):
    out = cc.help_for("/mute", "slack")
    assert "no room connection" in out and "telegram" in out
    assert cc.help_for("/mute", "telegram").startswith("/mute")


def test_no_transport_anywhere_drops_them_everywhere(monkeypatch):
    monkeypatch.setattr(rt, "_FACTORIES", {})
    monkeypatch.setattr(cc, "_BODY", {})
    assert not _names(cc.help_body("telegram")) & rt.ROOM_VERBS
    assert rt.seat_has_rooms("repl")   # not a chat surface: runs it through one that has rooms
