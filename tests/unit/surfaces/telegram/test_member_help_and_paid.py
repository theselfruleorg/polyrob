"""046: a room MEMBER's `/help` and `/paid`, driven as they arrive off the wire.

The two defects pinned here:

* a member's `/help` returned the OWNER's whole verb catalog and the 044
  owner-only redirect sent it to the OWNER's DM — the member saw nothing;
* the price list was readable only by the people who set it.
"""
import asyncio

import pytest

from core.surfaces.chat_policy import ChatPolicy
from core.surfaces.command_reply import reply_text, reply_to_room
from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import _handle_command
from surfaces.telegram.inbound import InboundResult


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



class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


def _result(text, *, chat_role, chat_type="supergroup", chat_id="-100",
            user_id="stranger"):
    source = SessionSource(surface_id="telegram", chat_id=chat_id,
                           chat_type=chat_type)
    identity = Identity(user_id=user_id, source=source, raw_user_id=user_id,
                        chat_role=chat_role)
    inbound = InboundMessage(text=text, identity=identity)
    decision = RouteDecision(
        kind=RouteKind.COMMAND,
        session_key=f"agent:main:telegram:{chat_type}:{chat_id}",
        session_id=None, command=text.split()[0])
    return InboundResult(inbound=inbound, decision=decision)


def _cmd(agent, result):
    return asyncio.run(_handle_command(agent, result, spawn=lambda c: None))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("ROOM_ACTIONS_ENABLED", "true")
    return tmp_path


@pytest.fixture
def priced(monkeypatch):
    from core.surfaces import room_actions
    policy = ChatPolicy(member_verbs=("help", "mute", "paid"), paid_enabled=True,
                        paid_mute_usd=0.5, paid_mute_max_duration="24h")
    monkeypatch.setattr(room_actions, "policy_for", lambda *a, **kw: policy)
    monkeypatch.setattr(room_actions, "_policy", lambda *a, **kw: policy)
    return policy


# --- /help -----------------------------------------------------------------

def test_a_members_help_goes_to_the_ROOM(env, priced):
    out = _cmd(_Agent(str(env)), _result("/help", chat_role="member"))
    assert reply_to_room(out) is True


def test_a_members_help_prices_this_room_and_hides_the_owner_catalog(env, priced):
    from surfaces.telegram.harness import _help_text
    out = reply_text(_cmd(_Agent(str(env)), _result("/help", chat_role="member")))
    assert "$0.50" in out
    assert "up to 24h" in out
    assert out != _help_text()
    assert "/allowlist" not in out


def test_the_owners_help_is_byte_identical(env, priced):
    from surfaces.telegram.harness import _help_text
    out = _cmd(_Agent(str(env)), _result("/help", chat_role="owner"))
    assert reply_to_room(out) is False
    assert out == _help_text()


def test_a_room_admins_help_is_byte_identical(env, priced):
    from surfaces.telegram.harness import _help_text
    out = _cmd(_Agent(str(env)), _result("/help", chat_role="admin"))
    assert out == _help_text()


def test_a_dm_help_is_byte_identical(env, priced):
    from surfaces.telegram.harness import _help_text
    out = _cmd(_Agent(str(env)),
               _result("/help", chat_role=None, chat_type="dm", chat_id="55"))
    assert out == _help_text()


def test_help_with_a_verb_still_works_for_the_owner(env, priced):
    from surfaces.telegram.harness import _help_for
    out = _cmd(_Agent(str(env)), _result("/help paid", chat_role="owner"))
    assert out == _help_for("paid")


def test_an_unreadable_room_does_not_fall_back_to_the_owner_catalog(env, priced,
                                                                    monkeypatch):
    """⚠️ Fail-open must not mean 'hand the member the admin verb list'."""
    from core.surfaces import room_actions
    from surfaces.telegram.harness import _help_text

    def _boom(*a, **kw):
        raise RuntimeError("store gone")

    monkeypatch.setattr(room_actions, "render_member_help", _boom)
    out = _cmd(_Agent(str(env)), _result("/help", chat_role="member"))
    assert reply_to_room(out) is True
    assert reply_text(out) != _help_text()
    assert "/allowlist" not in reply_text(out)


# --- /paid -----------------------------------------------------------------

def _paid(agent, result):
    from surfaces.telegram import group_ops
    return paid_reply(agent, result,
                                result.inbound.text.split()[1:])


def test_a_members_bare_paid_is_the_price_list_in_the_room(env, priced):
    out = _paid(_Agent(str(env)), _result("/paid", chat_role="member"))
    assert reply_to_room(out) is True
    assert "$0.50" in reply_text(out)


def test_a_members_paid_prices_is_the_same_read(env, priced):
    out = _paid(_Agent(str(env)), _result("/paid prices", chat_role="member"))
    assert reply_to_room(out) is True
    assert "$0.50" in reply_text(out)


def test_a_member_cannot_reach_a_configuration_verb(env, priced):
    for verb in ("status", "enable", "disable", "offers", "cancel", "asset",
                 "price"):
        out = _paid(_Agent(str(env)),
                    _result(f"/paid {verb}", chat_role="member"))
        text = reply_text(out)
        assert "price list" in text, verb
        assert "$" not in text, verb     # no configuration leaked


def test_the_owners_paid_status_is_unchanged(env, priced):
    """The owner's lane still renders `room_action_admin.status` verbatim —
    which reads the room's REAL overlay (the `priced` fixture patches only
    `room_actions`, so this room is genuinely unconfigured)."""
    from core.surfaces import room_action_admin as adm
    out = _paid(_Agent(str(env)), _result("/paid status", chat_role="owner"))
    assert reply_to_room(out) is False
    assert reply_text(out) == adm.status(_Container(str(env)), "telegram",
                                         "-100")


def test_the_owner_can_read_the_members_own_view(env, priced):
    out = _paid(_Agent(str(env)), _result("/paid prices", chat_role="owner"))
    assert reply_to_room(out) is False
    assert "$0.50" in reply_text(out)


# --- routing: the grant is still required ----------------------------------

def test_the_dispatcher_admits_paid_from_a_member_only_with_the_grant(env):
    """⚠️ The closed set alone is not the grant. A room that never listed `paid`
    leaves a member's `/paid` as ordinary room chatter, exactly as before."""
    from core.surfaces import chat_policy
    from core.surfaces.dispatcher import (_MEMBER_GRANTABLE_COMMANDS,
                                          _member_verb_granted)
    container = _Container(str(env))
    assert "/paid" in _MEMBER_GRANTABLE_COMMANDS
    assert not _member_verb_granted(container, "telegram", "-100", "/paid")
    ok, msg = chat_policy.set(str(env), "rob", "telegram", "-100",
                              "chat.member_verbs", ["help", "paid"])
    assert ok, msg
    assert _member_verb_granted(container, "telegram", "-100", "/paid")
