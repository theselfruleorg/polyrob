"""Configuring a room from the owner's DM.

The owner's ask (2026-09-15): the only thing that should have to be typed in
the public room is `/groups allow here`. Everything else — prices, caps, member
verbs, enabling the sale — is administration, and administration belongs in a
DM where a shoulder-surfer is not guaranteed.

Two things blocked that:

* `/paid` hard-refused a DM ("run it inside the group"), so a room's whole
  paid configuration could only be typed in front of its members.
* `here` was refused in a DM, and the explicit `<surface> <chat_id>` grammar
  means retyping `telegram -1002002374383` on every line.

⚠️ The fix must not widen anyone. A DM carries no `identity.chat_role` — it is
stamped only for the room a message arrived through — so the role for a
DM-targeted room is resolved from the roles store for THAT room
(`_resolve_role`, already correct for an explicit target). These tests pin that
the focus path goes through the same check, so a room admin cannot administer a
room he is only a member of by focusing it.
"""
import asyncio

import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.group_ops import groups_reply as _groups_reply_async
from surfaces.telegram.group_ops import paid_reply
from surfaces.telegram.inbound import InboundResult


# --- async-seam shim (2026-09-15) -------------------------------------------
# The Telegram verb handlers became `async def` so a bot read runs on the
# CALLER's event loop instead of being bridged to another one — the "got Future
# attached to a different loop" outage that made every target read as PROTECTED
# and the owner's free /ban do nothing. These tests drive them synchronously,
# exactly as this suite already wraps `groups_reply`.
import asyncio as _aio
from surfaces.telegram import group_ops as _gops


def _sync_seam(fn):
    def _call(*a, **k):
        r = fn(*a, **k)
        return _aio.run(r) if _aio.iscoroutine(r) else r
    return _call

paid_reply = _sync_seam(_gops.paid_reply)



def groups_reply(task_agent, result, args):
    return asyncio.run(_groups_reply_async(task_agent, result, args))


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


def _result(text, *, user_id="rob", raw_user_id=None, chat_role=None,
            chat_type="dm", chat_id="555"):
    source = SessionSource(surface_id="telegram", chat_id=chat_id, chat_type=chat_type)
    identity = Identity(user_id=user_id, source=source,
                        raw_user_id=raw_user_id or user_id, chat_role=chat_role)
    inbound = InboundMessage(text=text, identity=identity)
    decision = RouteDecision(kind=RouteKind.COMMAND,
                             session_key=f"agent:main:telegram:{chat_type}:{chat_id}",
                             session_id=None, command=text.split()[0])
    return InboundResult(inbound=inbound, decision=decision)


def _args(text):
    return text.split()[1:]


ROOM = "-1002002374383"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("ROOM_ACTIONS_ENABLED", "true")
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    return tmp_path


@pytest.fixture
def agent(env):
    return _Agent(str(env))


# --- the bare-key failure the owner actually hit ---------------------------

def test_a_bare_chat_key_is_accepted_as_the_guide_documents(agent, env):
    """⚠️ The guide says `/groups set here paid_ban_max_duration 6h`, and
    `chat_policy.set` refuses any key not starting with `chat.` — so the
    documented line was refused. A bare key is normalised, not rejected."""
    out = groups_reply(agent, _result("/groups set x y"),
                       ["set", "telegram", ROOM, "paid_ban_max_duration", "6h"])
    assert "❌" not in out
    assert "paid_ban_max_duration" in out

    from core.surfaces import chat_policy
    p = chat_policy.load(str(env), "rob", "telegram", ROOM)
    assert p.paid_ban_max_duration == "6h"


def test_a_prefixed_key_still_works_unchanged(agent, env):
    out = groups_reply(agent, _result("/groups set x y"),
                       ["set", "telegram", ROOM, "chat.paid_mute_usd", "0.10"])
    assert "❌" not in out
    from core.surfaces import chat_policy
    assert chat_policy.load(str(env), "rob", "telegram", ROOM).paid_mute_usd == 0.10


def test_a_genuinely_unknown_key_is_still_refused(agent, env):
    """Normalising must not turn an unknown key into a silently-written one."""
    out = groups_reply(agent, _result("/groups set x y"),
                       ["set", "telegram", ROOM, "not_a_real_key", "1"])
    assert "❌" in out


# --- the DM focus ----------------------------------------------------------

def test_use_binds_a_room_and_here_then_works_in_a_dm(agent, env):
    groups_reply(agent, _result("/groups use x"), ["use", "telegram", ROOM])
    out = groups_reply(agent, _result("/groups set here x"),
                       ["set", "here", "paid_mute_usd", "0.25"])
    assert "❌" not in out
    from core.surfaces import chat_policy
    assert chat_policy.load(str(env), "rob", "telegram", ROOM).paid_mute_usd == 0.25


def test_use_with_no_binding_still_explains_itself(agent, env):
    """⚠️ `here` in a DM with nothing focused must NOT silently pick a room."""
    out = groups_reply(agent, _result("/groups set here x"),
                       ["set", "here", "paid_mute_usd", "0.25"])
    assert "here" in out.lower()


def test_use_reports_and_clears(agent, env):
    groups_reply(agent, _result("/groups use x"), ["use", "telegram", ROOM])
    shown = groups_reply(agent, _result("/groups use"), ["use"])
    assert ROOM in shown
    groups_reply(agent, _result("/groups use none"), ["use", "none"])
    after = groups_reply(agent, _result("/groups set here x"),
                         ["set", "here", "paid_mute_usd", "0.25"])
    assert "here" in after.lower()


def test_being_in_a_room_always_beats_the_focus(agent, env):
    """A message sent IN a room means THAT room, whatever is focused."""
    groups_reply(agent, _result("/groups use x"), ["use", "telegram", ROOM])
    other = _result("/groups set here x", chat_type="supergroup",
                    chat_id="-1009999", chat_role="owner")
    out = groups_reply(agent, other, ["set", "here", "paid_mute_usd", "0.99"])
    assert "-1009999" in out

    from core.surfaces import chat_policy
    assert chat_policy.load(str(env), "rob", "telegram", ROOM).paid_mute_usd != 0.99


# --- /paid from a DM -------------------------------------------------------

def test_paid_status_works_from_a_dm_against_the_focused_room(agent, env):
    groups_reply(agent, _result("/groups use x"), ["use", "telegram", ROOM])
    out = paid_reply(agent, _result("/paid status"), ["status"])
    text = out if isinstance(out, str) else getattr(out, "text", str(out))
    assert "run it inside the group" not in text


def test_paid_in_a_dm_with_no_focus_names_the_remedy(agent, env):
    out = paid_reply(agent, _result("/paid status"), ["status"])
    text = out if isinstance(out, str) else getattr(out, "text", str(out))
    assert "/groups use" in text or "inside the group" in text


def test_paid_price_from_a_dm_writes_the_focused_room(agent, env):
    groups_reply(agent, _result("/groups use x"), ["use", "telegram", ROOM])
    paid_reply(agent, _result("/paid price mute 0.10"), ["price", "mute", "0.10"])
    from core.surfaces import chat_policy
    assert chat_policy.load(str(env), "rob", "telegram", ROOM).paid_mute_usd == 0.10


# --- the thing that must NOT widen ----------------------------------------

def test_a_non_owner_cannot_administer_a_room_by_focusing_it(agent, env):
    """⚠️ A DM carries no `chat_role`. Focusing a room must resolve the role
    from the roles store for THAT room — never inherit a stamp from elsewhere
    and never default to admin."""
    stranger = _result("/groups use x", user_id="mallory", raw_user_id="99")
    groups_reply(agent, stranger, ["use", "telegram", ROOM])
    out = groups_reply(agent, _result("/groups set here x", user_id="mallory",
                                      raw_user_id="99"),
                       ["set", "here", "paid_mute_usd", "9.99"])
    assert "🔒" in out

    from core.surfaces import chat_policy
    assert chat_policy.load(str(env), "rob", "telegram", ROOM).paid_mute_usd != 9.99


def test_the_focus_is_per_user_not_global(agent, env):
    """One person's focus must never steer another person's `here`."""
    groups_reply(agent, _result("/groups use x"), ["use", "telegram", ROOM])
    out = groups_reply(agent, _result("/groups set here x", user_id="mallory",
                                      raw_user_id="99"),
                       ["set", "here", "paid_mute_usd", "9.99"])
    assert "🔒" in out or "here" in out.lower()
