"""Proposal 125 S33 (D11): a non-owner, non-admin tenant's max_steps is clamped to
the interactive budget (no error), even with no credit billing on the box."""
from types import SimpleNamespace

import pytest

from agents.task.billed_request import bind_request_limits
from core.config_policy import interactive_max_steps


class _Tiers:
    def __init__(self, tier):
        self._tier = tier

    async def get_user_tier(self, user_id):
        return self._tier


class _Container:
    def __init__(self, tier=None):
        self._services = {"tier_manager": _Tiers(tier)} if tier else {}

    def get_service(self, name):
        return self._services.get(name)


def _req(steps):
    return SimpleNamespace(max_steps=steps, model=None, provider=None, tools=[],
                           session_config=None)


@pytest.fixture
def stranger(monkeypatch):
    import core.surfaces.owner_address as oa
    monkeypatch.setattr(oa, "is_owner_tenant", lambda uid: uid == "owner-1")


@pytest.mark.asyncio
async def test_stranger_is_clamped_without_billing(stranger):
    r = _req(100000)
    await bind_request_limits(_Container("free"), r, "stranger-1", None)
    assert r.max_steps == interactive_max_steps()


@pytest.mark.asyncio
async def test_stranger_without_tier_manager_is_clamped(stranger):
    r = _req(100000)
    await bind_request_limits(None, r, "stranger-1", None)
    assert r.max_steps == interactive_max_steps()


@pytest.mark.asyncio
async def test_owner_is_unchanged(stranger):
    r = _req(100000)
    await bind_request_limits(_Container("free"), r, "owner-1", None)
    assert r.max_steps == 100000


@pytest.mark.asyncio
async def test_admin_is_unchanged(stranger):
    r = _req(100000)
    await bind_request_limits(_Container("admin"), r, "admin-1", None)
    assert r.max_steps == 100000


@pytest.mark.asyncio
async def test_string_steps_clamped_to_int(stranger):
    r = _req("500")
    await bind_request_limits(_Container("free"), r, "stranger-1", None)
    assert r.max_steps == interactive_max_steps()
    assert isinstance(r.max_steps, int)


@pytest.mark.asyncio
async def test_small_value_is_kept(stranger):
    r = _req(5)
    await bind_request_limits(_Container("free"), r, "stranger-1", None)
    assert r.max_steps == 5


@pytest.mark.asyncio
async def test_autonomous_session_uses_autonomous_ceiling(stranger, monkeypatch):
    import agents.task.session_class as sc
    from core.config_policy import AutonomyConfig
    monkeypatch.setattr(sc, "is_autonomous_session", lambda sid: sid == "auto-1")
    r = _req(100000)
    await bind_request_limits(_Container("free"), r, "stranger-1", "auto-1")
    assert r.max_steps == AutonomyConfig.autonomous_max_steps()
