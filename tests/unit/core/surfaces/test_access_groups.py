"""Wave 3 Task 2 — group-chat access tiers (GROUP_CHAT_ENABLED).

Invariants:
- flag OFF (default): non-DM -> DENIED, byte-identical to v1;
- flag ON: non-allowlisted chat -> DENIED; owner in allowed chat -> OWNER;
  anyone else in allowed chat -> GROUP_PARTICIPANT;
- fail-closed: allowlist faults read as not-allowed; local-owner bypass is
  still refused on network surfaces inside groups.
"""
import tempfile

import pytest

from core.surfaces.access import AccessTier, resolve_access_tier
from core.surfaces.envelopes import Identity, SessionSource
from core.surfaces.group_allowlist import GroupAllowlist


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


def _identity(user_id, *, surface="discord", chat_id="chan-1",
              chat_type="group"):
    return Identity(
        user_id=user_id,
        source=SessionSource(surface_id=surface, chat_id=chat_id,
                             chat_type=chat_type),
        raw_user_id=user_id,
    )


@pytest.fixture()
def workdir():
    yield tempfile.mkdtemp()


def _allow(workdir, surface="discord", chat_id="chan-1"):
    import os
    GroupAllowlist(os.path.join(workdir, "group_allowlist.db")).allow(
        surface, chat_id)


def test_flag_off_group_denied_legacy(workdir):
    c = _Container(workdir)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_owner"), env=env) \
        == AccessTier.DENIED


def test_flag_on_unlisted_chat_denied(workdir):
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_owner"), env=env) \
        == AccessTier.DENIED


def test_flag_on_owner_in_allowed_chat_is_owner(workdir):
    _allow(workdir)
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_owner"), env=env) \
        == AccessTier.OWNER


def test_flag_on_stranger_in_allowed_chat_is_participant(workdir):
    _allow(workdir)
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_stranger"), env=env) \
        == AccessTier.GROUP_PARTICIPANT


def test_local_owner_bypass_refused_in_network_group(workdir):
    """POLYROB_LOCAL must not auto-own a forgeable group sender."""
    _allow(workdir)
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_LOCAL": "1"}
    assert resolve_access_tier(c, _identity("u_anyone"), env=env) \
        == AccessTier.GROUP_PARTICIPANT


def test_dm_flow_unchanged_by_group_flag(workdir):
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_owner", chat_type="dm"),
                               env=env) == AccessTier.OWNER


# --------------------------------------------------------------------------
# 013 T2 review, Finding 1: resolve_access_tier re-derived GROUP_CHAT_ENABLED
# from raw env via a local _BOOL_TRUE set, disagreeing with the dispatcher
# (which already routes through SurfaceConfig.group_chat_enabled(), the wired
# mode-aware getter) — so under autonomous mode with the env var unset, the
# dispatcher would let a group message through only for access.py to then
# DENY it anyway. These pin the unset-default now agreeing with the mode.
# --------------------------------------------------------------------------

def _enable_full(monkeypatch):
    """Copied from tests/unit/agents/task/test_autonomy_mode.py — activates
    effective autonomous mode via REAL process env (monkeypatch), independent
    of the `env=` mapping resolve_access_tier takes for its own fields."""
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    from agents.task import constants
    constants.reset_autonomy_mode_warnings()


def test_group_flag_unset_supervised_denied_by_mode_default(monkeypatch, workdir):
    """(a) supervised/unset -> disabled exactly as today."""
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    _allow(workdir)
    c = _Container(workdir)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}  # GROUP_CHAT_ENABLED unset
    assert resolve_access_tier(c, _identity("u_stranger"), env=env) \
        == AccessTier.DENIED


def test_group_flag_unset_autonomous_mode_enables_group_participant(monkeypatch, workdir):
    """(b) effective autonomous mode -> GROUP_CHAT_ENABLED's unset default flips
    ON, even though the env MAPPING passed to resolve_access_tier has no
    explicit key for it (mirrors production: dispatcher passes no override)."""
    _enable_full(monkeypatch)
    _allow(workdir)
    c = _Container(workdir)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}  # GROUP_CHAT_ENABLED still unset
    assert resolve_access_tier(c, _identity("u_stranger"), env=env) \
        == AccessTier.GROUP_PARTICIPANT


def test_group_flag_explicit_false_wins_over_autonomous_mode(monkeypatch, workdir):
    """(c) explicit env false wins over the mode default."""
    _enable_full(monkeypatch)
    _allow(workdir)
    c = _Container(workdir)
    env = {"POLYROB_OWNER_USER_ID": "u_owner", "GROUP_CHAT_ENABLED": "false"}
    assert resolve_access_tier(c, _identity("u_stranger"), env=env) \
        == AccessTier.DENIED


# --------------------------------------------------------------------------
# 044 T16: the tier carries the per-chat ROLE. `blocked` is the only
# per-member deny; the enum member is GROUP_MEMBER, with GROUP_PARTICIPANT
# kept as an alias for one release.
# --------------------------------------------------------------------------

def _roles(workdir):
    import os

    from core.surfaces.group_roles import GroupRoles
    return GroupRoles(os.path.join(workdir, "surfaces.db"))


def test_group_member_tier_name():
    assert AccessTier.GROUP_MEMBER.value == "group_member"
    assert AccessTier.GROUP_PARTICIPANT is AccessTier.GROUP_MEMBER


def test_blocked_member_is_denied(workdir):
    _allow(workdir)
    _roles(workdir).grant("discord", "chan-1", "u_bad", "blocked", granted_by="rob")
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_bad"), env=env) == AccessTier.DENIED


def test_blocked_owner_row_cannot_demote_the_owner(workdir):
    """The owner principal is resolved BEFORE any row is read, so a room admin
    cannot block the owner out of his own agent."""
    _allow(workdir)
    _roles(workdir).grant("discord", "chan-1", "u_owner", "blocked", granted_by="u_bad")
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_owner"), env=env) == AccessTier.OWNER


def test_chat_role_is_stamped_on_the_identity(workdir):
    _allow(workdir)
    _roles(workdir).grant("discord", "chan-1", "u_admin", "admin", granted_by="rob")
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}

    admin = _identity("u_admin")
    assert resolve_access_tier(c, admin, env=env) == AccessTier.GROUP_MEMBER
    assert admin.chat_role == "admin"

    plain = _identity("u_stranger")
    assert resolve_access_tier(c, plain, env=env) == AccessTier.GROUP_MEMBER
    assert plain.chat_role == "member"

    owner = _identity("u_owner")
    assert resolve_access_tier(c, owner, env=env) == AccessTier.OWNER
    assert owner.chat_role == "owner"


def test_roles_key_on_the_raw_platform_id(workdir):
    """`/groups role here 9911` names a TELEGRAM id, so the row must be found
    by the raw id, not the internal user_id it maps to."""
    from core.surfaces.envelopes import Identity, SessionSource

    _allow(workdir)
    _roles(workdir).grant("discord", "chan-1", "9911", "admin", granted_by="rob")
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}
    ident = Identity(user_id="u_internal",
                     source=SessionSource(surface_id="discord", chat_id="chan-1",
                                          chat_type="group"),
                     raw_user_id="9911")
    assert resolve_access_tier(c, ident, env=env) == AccessTier.GROUP_MEMBER
    assert ident.chat_role == "admin"


def test_a_role_store_fault_reads_as_blocked_never_as_member(workdir, monkeypatch):
    """D3: fail-CLOSED to ``blocked``, not to ``member``.

    Answering ``member`` was called "least privilege" and is not: ``blocked``
    is the room's ONLY per-member deny and it lives in the very store this read
    consults, so an unreadable store un-blocked everyone an owner had blocked.
    The owner principal is resolved before any row is read, so this can never
    lock the owner out of his own room.
    """
    import core.surfaces.access as access
    _allow(workdir)
    _roles(workdir).grant("discord", "chan-1", "u_admin", "admin", granted_by="rob")

    import core.surfaces.group_roles as gr

    def _boom(*a, **kw):
        raise RuntimeError("role store unreadable")

    monkeypatch.setattr(gr.GroupRoles, "role", _boom)
    access._ROLE_FAULT_WARNED.clear()
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}
    ident = _identity("u_admin")
    assert resolve_access_tier(c, ident, env=env) == AccessTier.DENIED
    assert ident.chat_role == "blocked"


def test_a_role_store_fault_never_locks_the_owner_out(workdir, monkeypatch):
    """The other half of D3: the owner is resolved from the PRINCIPAL, before
    any row is read, so the fail-closed role read cannot reach him."""
    import core.surfaces.access as access
    import core.surfaces.group_roles as gr
    _allow(workdir)

    def _boom(*a, **kw):
        raise RuntimeError("role store unreadable")

    monkeypatch.setattr(gr.GroupRoles, "role", _boom)
    access._ROLE_FAULT_WARNED.clear()
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}
    ident = _identity("u_owner")
    assert resolve_access_tier(c, ident, env=env) == AccessTier.OWNER
    assert ident.chat_role == "owner"


def test_a_paired_user_is_a_room_MEMBER_not_the_room_owner(workdir):
    """044 T16 fix round 1. A pairing row says "this person may talk to the
    agent" — a DM-scoped grant. Reading it as room OWNERSHIP handed any paired
    user the steer frame, media absorption, the lifecycle verbs and immunity to
    `blocked` in every allowlisted room. In a room the owner is the bound
    PRINCIPAL and nobody else."""
    import os

    from core.pairing import PairingStore

    _allow(workdir)
    store = PairingStore(os.path.join(workdir, "pairing.db"))
    store.approve(store.request("u_paired"))
    assert store.is_paired("u_paired")
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}

    ident = _identity("u_paired")
    assert resolve_access_tier(c, ident, env=env) == AccessTier.GROUP_MEMBER
    assert ident.chat_role == "member"

    # …and a room row can still promote him, which is the supported path.
    _roles(workdir).grant("discord", "chan-1", "u_paired", "admin", granted_by="u_owner")
    promoted = _identity("u_paired")
    assert resolve_access_tier(c, promoted, env=env) == AccessTier.GROUP_MEMBER
    assert promoted.chat_role == "admin"


def test_a_paired_user_is_still_the_owner_in_a_DM(workdir):
    """The DM branch is unchanged: pairing is exactly the grant it was built to
    be, and narrowing it there would lock out every paired user."""
    import os

    from core.pairing import PairingStore

    store = PairingStore(os.path.join(workdir, "pairing.db"))
    store.approve(store.request("u_paired"))
    c = _Container(workdir)
    env = {"GROUP_CHAT_ENABLED": "true", "POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_paired", chat_type="dm"), env=env) \
        == AccessTier.OWNER
