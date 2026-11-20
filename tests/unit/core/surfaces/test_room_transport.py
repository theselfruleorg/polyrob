"""The room transport seam (proposal 125 S14): the room verbs' body runs below the
seats and reaches the chat platform only through ``core.surfaces.room_transport``."""
import ast
import asyncio
from pathlib import Path

import pytest

from core.surfaces import room_transport as rt

_REPO = Path(__file__).resolve().parents[4]


def test_an_unregistered_surface_gets_the_null_transport_and_it_fails_closed():
    t = rt.room_transport("nowhere-surface", object())
    assert isinstance(t, rt.NullRoomTransport)
    assert t.connected() is False
    assert t.reply_target({"message": {}}) is None
    assert t.room_title({"message": {"chat": {"title": "x"}}}) == ""
    assert asyncio.run(t.rights("-1")) == set()           # no rights: a sale refuses
    with pytest.raises(RuntimeError):                     # unreadable: PROTECTED
        asyncio.run(t.member_status("-1", "7"))
    res = asyncio.run(t.apply(object(), object(), 0))     # not applied: a credit
    assert res.ok is False and res.reason
    assert asyncio.run(t.suggest_admins("-1")) is None    # cannot list here


def test_a_factory_that_raises_is_logged_and_reads_as_no_connection():
    def _boom(_agent):
        raise ValueError("no")
    rt.register_room_transport("test-broken-surface", _boom)
    assert isinstance(rt.room_transport("test-broken-surface"), rt.NullRoomTransport)


def test_registration_is_by_surface_and_case_blind():
    class _T(rt.NullRoomTransport):
        def connected(self):
            return True
    rt.register_room_transport("Test-Surface-X", lambda agent: _T())
    assert rt.room_transport("test-surface-x").connected() is True
    assert "test-surface-x" in rt.registered_surfaces()


def test_telegram_registers_its_transport_on_import():
    import surfaces.telegram.group_ops as g
    t = rt.room_transport("telegram", None)
    assert isinstance(t, g.TelegramRoomTransport)
    assert isinstance(t, rt.RoomTransport)


def test_the_room_verbs_body_imports_no_chat_platform():
    """The decision body asks the transport; it never imports a seat."""
    tree = ast.parse((_REPO / "cron/owner_rooms.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("surfaces"), node.module
        if isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("surfaces"), a.name
