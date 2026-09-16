"""`/paid` — the Telegram seat over room_action_admin (046).

⚠️ A chat verb needs FOUR lists or its handler is dead: the dispatch branch,
_OWNER_ADMIN_COMMANDS, the help body, and a deliberate decision about
_ROOM_REFUSED_COMMANDS. This file pins all four.
"""
import pytest

from surfaces.telegram import group_ops


# --- async-seam shim (2026-09-15) -------------------------------------------
# The Telegram verb handlers became `async def` so a bot read runs on the
# CALLER's event loop instead of being bridged to another one — the "got Future
# attached to a different loop" outage that made every target read as PROTECTED
# and the owner's free /ban do nothing. Driven synchronously here, exactly as
# this suite already wraps `groups_reply`.
import asyncio as _aio
from surfaces.telegram import group_ops as _gops


def _sync_seam(fn):
    def _call(*a, **k):
        r = fn(*a, **k)
        return _aio.run(r) if _aio.iscoroutine(r) else r
    return _call

paid_reply = _sync_seam(_gops.paid_reply)



def _result(*, text, user_id="2277", chat_id="-100123",
            chat_type="supergroup", chat_role="owner"):
    src = type("Src", (), {"surface_id": "telegram", "chat_id": chat_id,
                           "chat_type": chat_type})()
    identity = type("Id", (), {"user_id": user_id, "raw_user_id": user_id,
                               "source": src, "chat_role": chat_role})()
    inbound = type("In", (), {"text": text, "identity": identity,
                              "raw": {"message": {"chat": {"id": chat_id}}}})()
    return type("Res", (), {"inbound": inbound})()


class _Agent:
    def __init__(self, tmp_path):
        self.container = type("C", (), {
            "config": type("Cfg", (), {"data_dir": str(tmp_path)})(),
            "get_service": lambda self, n: None})()


@pytest.fixture(autouse=True)
def _owner(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: "u_owner")


# --- the four lists ---------------------------------------------------------

def test_paid_is_in_the_owner_admin_command_tuple():
    from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS
    assert "/paid" in _OWNER_ADMIN_COMMANDS


def test_paid_is_in_the_help_body():
    from surfaces.telegram.harness import _HELP_BODY
    assert "/paid" in _HELP_BODY


def test_paid_is_NOT_room_refused():
    """⚠️ Deliberate: it configures THIS room and names no money the agent can
    move. Refusing it would send an admin to a DM to configure the room he is
    standing in."""
    from surfaces.telegram.harness import _ROOM_REFUSED_COMMANDS
    assert "/paid" not in _ROOM_REFUSED_COMMANDS


def test_a_room_admins_paid_line_routes_as_a_command():
    from core.surfaces.dispatcher import _GROUP_ADMIN_COMMANDS
    assert "/paid" in _GROUP_ADMIN_COMMANDS


# --- the verb ---------------------------------------------------------------

def test_an_unknown_verb_echoes_the_vocabulary(tmp_path):
    out = paid_reply(_Agent(tmp_path), _result(text="/paid nope"),
                               ["nope"])
    assert "nope" in out and "status" in out


def test_default_verb_is_status(tmp_path):
    out = paid_reply(_Agent(tmp_path), _result(text="/paid"), [])
    assert "not enabled" in out.lower()


def test_a_dm_is_refused_because_it_configures_a_room(tmp_path):
    out = paid_reply(
        _Agent(tmp_path), _result(text="/paid", chat_type="dm"), [])
    assert "inside the group" in out


def test_a_member_gets_the_price_list_not_the_configuration(tmp_path):
    """046: a member's `/paid` used to be a flat refusal, so the price list was
    readable only by the people who set it. He now gets THIS room's prices, in
    the room — and still nothing the owner configures."""
    from core.surfaces.command_reply import reply_text, reply_to_room
    out = paid_reply(
        _Agent(tmp_path), _result(text="/paid", chat_role="member"), [])
    assert reply_to_room(out) is True
    text = reply_text(out)
    assert "🔒" not in text
    for owner_word in ("caps:", "member verbs:", "awaiting payment", "asset"):
        assert owner_word not in text


def test_a_member_cannot_reach_a_paid_configuration_verb(tmp_path):
    from core.surfaces.command_reply import reply_text, reply_to_room
    for verb in ("status", "enable", "disable", "offers", "asset", "price",
                 "cancel"):
        out = paid_reply(
            _Agent(tmp_path), _result(text=f"/paid {verb}", chat_role="member"),
            [verb])
        assert reply_to_room(out) is True, verb
        assert "price list" in reply_text(out), verb


def test_an_admin_may_read_but_not_price(tmp_path):
    agent, res = _Agent(tmp_path), _result(text="/paid", chat_role="admin")
    assert "🔒" not in paid_reply(agent, res, ["status"])
    assert "Owner only" in paid_reply(
        agent, _result(text="/paid price mute 1", chat_role="admin"),
        ["price", "mute", "1"])


def test_the_owner_can_price_and_enable(tmp_path):
    agent = _Agent(tmp_path)
    assert "✅" in paid_reply(
        agent, _result(text="/paid price mute 0.50"), ["price", "mute", "0.50"])
    assert "✅" in paid_reply(
        agent, _result(text="/paid enable"), ["enable"])


def test_a_malformed_price_is_refused_not_coerced(tmp_path):
    out = paid_reply(
        _Agent(tmp_path), _result(text="/paid price mute free"),
        ["price", "mute", "free"])
    assert "❌" in out and "free" in out


def test_usage_is_shown_for_an_incomplete_verb(tmp_path):
    agent = _Agent(tmp_path)
    assert "Usage:" in paid_reply(agent, _result(text="/paid price"),
                                            ["price"])
    assert "Usage:" in paid_reply(agent, _result(text="/paid asset"),
                                            ["asset"])
    assert "Usage:" in paid_reply(agent, _result(text="/paid cancel"),
                                            ["cancel"])


def test_paid_is_routable_at_all():
    """⚠️ The FIFTH list. A verb handled but absent from `_COMMANDS` never
    becomes a COMMAND route — the handler is unreachable and only a test that
    builds the RouteDecision by hand would pass."""
    from core.surfaces.dispatcher import _COMMANDS
    assert "/paid" in _COMMANDS
