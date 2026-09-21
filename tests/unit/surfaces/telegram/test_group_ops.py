"""044 T18: `/groups` and `/mute` role gating — driven with real `InboundResult`
fixtures for an owner, a room admin and a plain member, exactly as they would
arrive off the wire (identity.chat_role stamped by `core/surfaces/access.py` at
the routing boundary for a non-DM turn)."""
import asyncio

import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.group_ops import groups_reply as _groups_reply_async
from surfaces.telegram.group_ops import mute_reply
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

mute_reply = _sync_seam(_gops.mute_reply)



def groups_reply(task_agent, result, args):
    """`groups_reply` is `async def` (T19: the `admins` verb awaits Telegram's
    own `get_chat_administrators`) — this is the sync-call shim every test
    below drives, mirroring how the real harness `await`s it."""
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
           chat_type="supergroup", chat_id="-100"):
    source = SessionSource(surface_id="telegram", chat_id=chat_id, chat_type=chat_type)
    identity = Identity(user_id=user_id, source=source,
                        raw_user_id=raw_user_id or user_id, chat_role=chat_role)
    inbound = InboundMessage(text=text, identity=identity)
    decision = RouteDecision(kind=RouteKind.COMMAND, session_key=f"agent:main:telegram:{chat_type}:{chat_id}",
                             session_id=None, command=text.split()[0])
    return InboundResult(inbound=inbound, decision=decision)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    return tmp_path


def _args(text):
    return text.split()[1:]


# ---------------------------------------------------------------------------
# /groups allow — owner only
# ---------------------------------------------------------------------------

def test_owner_allow_here_succeeds(env):
    agent = _Agent(str(env))
    result = _result("/groups allow here", chat_role="owner")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert "allowed" in out.lower()


def test_member_allow_here_is_refused(env):
    agent = _Agent(str(env))
    result = _result("/groups allow here", chat_role="member")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert out == "🔒 Owner only."


def test_admin_allow_here_is_refused(env):
    agent = _Agent(str(env))
    result = _result("/groups allow here", chat_role="admin")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert out == "🔒 Owner only."


def test_dm_owner_resolved_without_a_stamped_chat_role(env):
    """A DM never carries `chat_role` (044 T16 only stamps it for a group
    turn) — the owner check falls back to `_is_admin_owner`."""
    agent = _Agent(str(env))
    result = _result("/groups list", chat_role=None, chat_type="dm", chat_id="1")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert "no group chats" in out.lower()


# ---------------------------------------------------------------------------
# /groups mode — owner OR room admin
# ---------------------------------------------------------------------------

def test_admin_mode_here_succeeds(env):
    agent = _Agent(str(env))
    groups_reply(agent, _result("/groups allow here", chat_role="owner"),
                _args("/groups allow here"))
    result = _result("/groups mode here listen", chat_role="admin")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert "listen" in out


def test_member_mode_here_is_refused(env):
    agent = _Agent(str(env))
    result = _result("/groups mode here active", chat_role="member")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert out == "🔒 Owner or room admin only."


def test_bad_mode_names_the_vocabulary(env):
    agent = _Agent(str(env))
    result = _result("/groups mode here loud", chat_role="owner")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert "mention, active, listen, off" in out


# ---------------------------------------------------------------------------
# /groups role — owner any role; admin may grant only `blocked`
# ---------------------------------------------------------------------------

def test_admin_may_block_a_member(env):
    agent = _Agent(str(env))
    result = _result("/groups role here 9911 blocked", chat_role="admin")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert "blocked" in out.lower()
    assert "🔒" not in out


def test_admin_may_not_promote_to_admin(env):
    agent = _Agent(str(env))
    result = _result("/groups role here 9911 admin", chat_role="admin")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert out == "🔒 Owner only."


def test_owner_may_grant_admin(env):
    agent = _Agent(str(env))
    result = _result("/groups role here 9911 admin", chat_role="owner")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert "admin" in out.lower() and "🔒" not in out


# ---------------------------------------------------------------------------
# /groups set, service, tail — owner-only except tail
# ---------------------------------------------------------------------------

def test_admin_set_is_refused(env):
    agent = _Agent(str(env))
    result = _result("/groups set here chat.tone playful", chat_role="admin")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert out == "🔒 Owner only."


def test_admin_service_is_refused(env):
    agent = _Agent(str(env))
    result = _result("/groups service here", chat_role="admin")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert out == "🔒 Owner only."


def test_admin_tail_succeeds_when_empty(env):
    """D58: a READ never CREATES the store. With no ``surfaces.db`` on the box
    the answer says the room log does not exist yet — never "no ledger rows",
    which is a claim about a store nobody opened."""
    import os as _os
    agent = _Agent(str(env))
    result = _result("/groups tail here", chat_role="admin")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert "no room log on this box yet" in out.lower()
    assert not _os.path.exists(_os.path.join(str(env), "surfaces.db"))


def test_unknown_verb_names_the_vocabulary(env):
    agent = _Agent(str(env))
    result = _result("/groups frobnicate here", chat_role="owner")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert "unknown" in out.lower() and "allow" in out.lower()


# ---------------------------------------------------------------------------
# /mute — owner OR room admin
# ---------------------------------------------------------------------------

def test_owner_mute_here_succeeds(env):
    agent = _Agent(str(env))
    result = _result("/mute here 2h", chat_role="owner")
    out = mute_reply(agent, result, _args(result.inbound.text))
    assert "muted" in out.lower() and "2h" in out


def test_admin_mute_here_succeeds(env):
    agent = _Agent(str(env))
    result = _result("/mute here 30m", chat_role="admin")
    out = mute_reply(agent, result, _args(result.inbound.text))
    assert "muted" in out.lower()


def test_member_mute_here_is_refused(env):
    agent = _Agent(str(env))
    result = _result("/mute here 1h", chat_role="member")
    out = mute_reply(agent, result, _args(result.inbound.text))
    assert out == "🔒 Owner or room admin only."


def test_mute_needs_a_duration(env):
    agent = _Agent(str(env))
    result = _result("/mute here", chat_role="owner")
    out = mute_reply(agent, result, _args(result.inbound.text))
    assert "usage" in out.lower()


# ---------------------------------------------------------------------------
# 044 T18 fix round 1
# ---------------------------------------------------------------------------

def test_dm_here_is_refused_for_groups_even_for_the_owner(env):
    """Important 3: `here` resolves from `identity.source`, which for a DM
    IS the DM — without this, `/groups allow here` from a DM would silently
    allowlist the owner's own private chat as a 'group'."""
    agent = _Agent(str(env))
    result = _result("/groups allow here", chat_role=None, chat_type="dm", chat_id="1")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert "here" in out.lower() and "dm" in out.lower()
    from core.surfaces.group_allowlist import GroupAllowlist
    assert not GroupAllowlist(str(env / "group_allowlist.db")).is_allowed("telegram", "1")


def test_dm_here_is_refused_for_mute(env):
    agent = _Agent(str(env))
    result = _result("/mute here 1h", chat_role="owner", chat_type="dm", chat_id="1")
    out = mute_reply(agent, result, _args(result.inbound.text))
    assert "here" in out.lower() and "dm" in out.lower()


def test_explicit_target_from_a_dm_still_works(env):
    """The refusal is specific to `here`, not to a DM using `/groups` at all."""
    agent = _Agent(str(env))
    result = _result("/groups list", chat_role=None, chat_type="dm", chat_id="1")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert "no group chats" in out.lower()


def test_admin_dm_explicit_target_grammar_succeeds(env):
    """Critical 1b: a DM never stamps `identity.chat_role` — the explicit
    `<surface> <chat_id>` grammar must resolve the sender's role for THAT
    room via `group_roles`, not silently default him to `member`."""
    from core.surfaces.group_roles import GroupRoles
    GroupRoles(str(env / "surfaces.db")).grant("telegram", "-100", "9911", "admin",
                                               granted_by="rob")
    agent = _Agent(str(env))
    result = _result("/groups mode telegram -100 listen", chat_role=None,
                     chat_type="dm", chat_id="1", user_id="u_admin", raw_user_id="9911")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert "listen" in out and "🔒" not in out


def test_member_dm_explicit_target_grammar_is_refused(env):
    agent = _Agent(str(env))
    result = _result("/groups mode telegram -100 listen", chat_role=None,
                     chat_type="dm", chat_id="1", user_id="u_stranger", raw_user_id="42")
    out = groups_reply(agent, result, _args(result.inbound.text))
    assert out == "🔒 Owner or room admin only."


def test_admin_dm_explicit_target_grammar_for_mute(env):
    from core.surfaces.group_roles import GroupRoles
    GroupRoles(str(env / "surfaces.db")).grant("telegram", "-100", "9911", "admin",
                                               granted_by="rob")
    agent = _Agent(str(env))
    result = _result("/mute telegram -100 2h", chat_role=None, chat_type="dm",
                     chat_id="1", user_id="u_admin", raw_user_id="9911")
    out = mute_reply(agent, result, _args(result.inbound.text))
    assert "muted" in out.lower()


# ---------------------------------------------------------------------------
# 044 T20: /groups service creates, reports and stops the room service job
# ---------------------------------------------------------------------------

def test_owner_service_creates_the_job_and_off_stops_it(env):
    agent = _Agent(str(env))

    def _run(text):
        result = _result(text, chat_role="owner")
        return groups_reply(agent, result, _args(result.inbound.text))

    # 044 T20 fix round 1 (Minor 10): a room the agent is not IN is refused —
    # the job would bind to an ingress-DENIED chat and skip forever.
    assert "not an allowed room" in _run("/groups service here")
    _run("/groups allow here The Den")

    out = _run("/groups service here every 1h max 2")
    assert "1h" in out and "telegram:-100" in out
    from cron.jobs import CronJobStore
    jobs = CronJobStore(str(env / "cron.db")).list()
    assert len(jobs) == 1 and jobs[0].payload["max_replies"] == 2

    assert "already" in _run("/groups service here").lower()
    assert "stopped" in _run("/groups service here off").lower()
    assert [j.status for j in CronJobStore(str(env / "cron.db")).list()] == ["cancelled"]


# ---------------------------------------------------------------------------
# 044 C4 — a room admin is an admin of HIS room, not of every room
# ---------------------------------------------------------------------------

def test_admin_of_one_room_is_a_member_of_another(env):
    """The stamped `identity.chat_role` describes the room the message ARRIVED
    through. `_resolve_role` returned it unconditionally, so an admin of AAA
    naming BBB explicitly was treated as BBB's admin too — `/groups mode
    telegram <bbb> off` from one room could silence another."""
    agent = _Agent(str(env))
    # Speaking IN room AAA, stamped admin there, targeting room BBB explicitly.
    result = _result("/groups mode telegram -200 off", user_id="mallory",
                     chat_role="admin", chat_id="-100")
    out = groups_reply(agent, result, _args("/groups mode telegram -200 off"))
    assert "Owner or room admin only" in out


def test_admin_of_a_room_still_governs_that_room_by_explicit_name(env):
    """The other half: naming his OWN room explicitly must keep working."""
    from core.surfaces.group_roles import GroupRoles
    import os
    GroupRoles(os.path.join(str(env), "surfaces.db")).grant(
        "telegram", "-100", "mallory", "admin", granted_by="owner")
    agent = _Agent(str(env))
    result = _result("/groups mode telegram -100 listen", user_id="mallory",
                     chat_role="admin", chat_id="-100")
    out = groups_reply(agent, result, _args("/groups mode telegram -100 listen"))
    assert "Owner or room admin only" not in out


def test_admin_here_is_unchanged(env):
    """`here` resolves to the room the line arrived in, so the stamped role is
    the right answer and this path must not have regressed."""
    agent = _Agent(str(env))
    result = _result("/groups mode here listen", user_id="mallory",
                     chat_role="admin", chat_id="-100")
    out = groups_reply(agent, result, _args("/groups mode here listen"))
    assert "Owner or room admin only" not in out
