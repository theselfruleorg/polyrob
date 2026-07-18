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
