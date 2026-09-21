"""D2 + D14: a room ADMIN may grant exactly `blocked`, and nothing else.

Two halves of one hole, which only bite together:

* **D2** — the gate read ``rest[-1]`` for the role being granted while the grant
  itself wrote ``tail_args[1]``. So ``/groups role here 123 admin blocked``
  showed the gate a trailing ``blocked`` and handed the store an ``admin``: a
  room admin promoting himself with one extra word.
* **D14** — the harness forwarded only ``mode`` and ``tail`` to `group_ops`
  before the owner-only gate, so an admin's ``role`` line never reached the
  carve-out at all. That is what kept D2 latent — and it also meant the
  documented "an admin may block a disruptive member" did not exist on
  Telegram.
"""
import asyncio

import pytest

from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.dispatcher import RouteDecision, RouteKind
from surfaces.telegram import group_ops
from surfaces.telegram.inbound import InboundResult


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


def _result(text, *, chat_role="admin", user_id="member-7", chat_id="-100"):
    source = SessionSource(surface_id="telegram", chat_id=chat_id,
                           chat_type="supergroup")
    identity = Identity(user_id=user_id, source=source, raw_user_id=user_id,
                        chat_role=chat_role)
    return InboundResult(
        inbound=InboundMessage(text=text, identity=identity),
        decision=RouteDecision(kind=RouteKind.COMMAND,
                               session_key=f"agent:main:telegram:supergroup:{chat_id}",
                               command="/groups"))


def _run(agent, result):
    args = result.inbound.text.strip().split()[1:]
    return asyncio.run(group_ops.groups_reply(agent, result, args))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


def _role_of(env, user_id, chat_id="-100"):
    import os
    from core.surfaces.group_roles import GroupRoles
    return GroupRoles(os.path.join(str(env), "surfaces.db")).role(
        "telegram", chat_id, user_id, is_owner=False)


# --- D2 ---------------------------------------------------------------------

def test_an_admin_may_grant_blocked(env):
    out = _run(_Agent(str(env)), _result("/groups role here 123 blocked"))
    assert "blocked" in out and "🔒" not in out
    assert _role_of(env, "123") == "blocked"


def test_an_admin_may_not_grant_admin(env):
    out = _run(_Agent(str(env)), _result("/groups role here 123 admin"))
    assert "Owner only" in out
    assert _role_of(env, "123") == "member"


def test_a_trailing_blocked_never_buys_an_admin_grant(env):
    """D2 EXACTLY. The gate and the grant must read the SAME token; they read
    opposite ends of the line, so one extra word promoted the caller."""
    out = _run(_Agent(str(env)),
               _result("/groups role here 123 admin blocked"))
    assert "Owner only" in out
    assert _role_of(env, "123") == "member", (
        "a trailing word bought an admin grant — the gate and the grant read "
        "different tokens")


def test_a_trailing_blocked_never_buys_a_member_grant(env):
    """The same shape, un-blocking somebody an owner blocked."""
    from core.surfaces.group_roles import GroupRoles
    import os
    GroupRoles(os.path.join(str(env), "surfaces.db")).grant(
        "telegram", "-100", "123", "blocked", granted_by="rob")
    out = _run(_Agent(str(env)),
               _result("/groups role here 123 member blocked"))
    assert "Owner only" in out
    assert _role_of(env, "123") == "blocked"


def test_a_plain_member_may_grant_nothing(env):
    out = _run(_Agent(str(env)),
               _result("/groups role here 123 blocked", chat_role="member"))
    assert "Owner only" in out
    assert _role_of(env, "123") == "member"


def test_the_owner_may_still_grant_admin(env):
    out = _run(_Agent(str(env)),
               _result("/groups role here 123 admin", chat_role="owner",
                       user_id="rob"))
    assert "admin" in out and "🔒" not in out
    assert _role_of(env, "123") == "admin"


# --- D14 --------------------------------------------------------------------

def test_the_harness_forwards_role_before_the_owner_gate():
    """The routing half. `_handle_owner_admin` owner-gates everything that is
    not forwarded first, so `role` must be in the pre-gate set or the carve-out
    above is unreachable from a real Telegram message."""
    import inspect
    from surfaces.telegram import harness
    src = inspect.getsource(harness._handle_owner_admin)
    assert '"mode", "tail", "role"' in src, (
        "the pre-gate forward must include `role`, or an admin's `blocked` "
        "grant never reaches group_ops")


@pytest.mark.asyncio
async def test_an_admins_blocked_grant_survives_the_owner_gate(env):
    """End-to-end through the harness, as a real admin's line arrives."""
    from surfaces.telegram.harness import _handle_owner_admin
    out = await _handle_owner_admin(_Agent(str(env)),
                                    _result("/groups role here 123 blocked"),
                                    "/groups")
    assert "Owner only" not in out
    assert _role_of(env, "123") == "blocked"


@pytest.mark.asyncio
async def test_an_admins_admin_grant_is_still_refused_end_to_end(env):
    from surfaces.telegram.harness import _handle_owner_admin
    out = await _handle_owner_admin(_Agent(str(env)),
                                    _result("/groups role here 123 admin"),
                                    "/groups")
    assert "Owner only" in out
    assert _role_of(env, "123") == "member"
