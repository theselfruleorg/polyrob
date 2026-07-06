"""Telegram owner-admin verbs: /allow /deny /allowlist over OutboundAllowlist.

Mirrors test_owner_admin_commands.py — owner-gated by principal (network surface,
no local bypass); a non-owner gets a refusal and the store is never mutated.
"""
import os

import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.outbound_allowlist import OutboundAllowlist
from surfaces.telegram.harness import act_on_inbound, _OWNER_ADMIN_COMMANDS
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


def _cmd(command, text, user="gleb"):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text=text,
                             identity=Identity(user_id=user, source=src, raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:" + user, command=command))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "gleb")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    return tmp_path


def test_allow_verbs_are_owner_admin_commands():
    for verb in ("/allow", "/deny", "/allowlist"):
        assert verb in _OWNER_ADMIN_COMMANDS


@pytest.mark.asyncio
async def test_allow_refused_for_non_owner(env):
    out = await act_on_inbound(_Agent(str(env)),
                               _cmd("/allow", "/allow telegram 555", user="u_stranger"))
    assert "owner" in out.lower()
    store = OutboundAllowlist(os.path.join(str(env), "surfaces.db"))
    assert store.is_allowed("gleb", "telegram", "555") is False
    assert store.is_allowed("u_stranger", "telegram", "555") is False


@pytest.mark.asyncio
async def test_allow_by_owner_grants_target(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/allow", "/allow telegram 555"))
    assert "allow" in out.lower()
    store = OutboundAllowlist(os.path.join(str(env), "surfaces.db"))
    assert store.is_allowed("gleb", "telegram", "555") is True


@pytest.mark.asyncio
async def test_deny_by_owner_revokes_target(env):
    store = OutboundAllowlist(os.path.join(str(env), "surfaces.db"))
    store.allow("gleb", "telegram", "555")
    out = await act_on_inbound(_Agent(str(env)), _cmd("/deny", "/deny telegram 555"))
    assert "denied" in out.lower() or "revoked" in out.lower()
    assert store.is_allowed("gleb", "telegram", "555") is False


@pytest.mark.asyncio
async def test_deny_refused_for_non_owner(env):
    store = OutboundAllowlist(os.path.join(str(env), "surfaces.db"))
    store.allow("gleb", "telegram", "555")
    out = await act_on_inbound(_Agent(str(env)),
                               _cmd("/deny", "/deny telegram 555", user="u_stranger"))
    assert "owner" in out.lower()
    assert store.is_allowed("gleb", "telegram", "555") is True


@pytest.mark.asyncio
async def test_allowlist_lists_for_owner(env):
    store = OutboundAllowlist(os.path.join(str(env), "surfaces.db"))
    store.allow("gleb", "telegram", "555", note="team")
    out = await act_on_inbound(_Agent(str(env)), _cmd("/allowlist", "/allowlist"))
    assert "555" in out


@pytest.mark.asyncio
async def test_allowlist_empty_for_owner(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/allowlist", "/allowlist"))
    assert "no" in out.lower()


@pytest.mark.asyncio
async def test_allowlist_refused_for_non_owner(env):
    store = OutboundAllowlist(os.path.join(str(env), "surfaces.db"))
    store.allow("gleb", "telegram", "555")
    out = await act_on_inbound(_Agent(str(env)),
                               _cmd("/allowlist", "/allowlist", user="u_stranger"))
    assert "owner" in out.lower()


@pytest.mark.asyncio
async def test_allow_usage_message_on_missing_args(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/allow", "/allow"))
    assert "usage" in out.lower()
