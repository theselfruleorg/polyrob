"""G-1 (metering finalization): the orchestrator construction seam seeds the
owner/local user_profiles row(s) at most ONCE per process (not once per
session — SessionOrchestrator is constructed on every session create), guarded
by a module-level once-flag. modules.database.user_profiles.ensure_owner_profile
itself is idempotent, but re-running it on every session would still add a DB
round trip to every session construction; the once-flag avoids that.

See agents/task/agent/orchestrator.py::_maybe_seed_owner_profile.
"""
import asyncio

import pytest

from agents.task.agent import orchestrator as orch_mod


@pytest.fixture(autouse=True)
def _reset_seed_flag():
    orch_mod._owner_profile_seed_scheduled = False
    yield
    orch_mod._owner_profile_seed_scheduled = False


@pytest.mark.asyncio
async def test_seed_helper_runs_at_most_once_per_process(monkeypatch):
    calls = []

    async def _fake_ensure_owner_profile(db=None):
        calls.append(db)
        return True

    monkeypatch.setattr(
        "modules.database.user_profiles.ensure_owner_profile",
        _fake_ensure_owner_profile,
    )

    db_obj = object()
    orch_mod._maybe_seed_owner_profile(db_obj)
    orch_mod._maybe_seed_owner_profile(db_obj)
    orch_mod._maybe_seed_owner_profile(db_obj)

    # Let the fire-and-forget scheduled task(s) actually run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_seed_helper_noop_without_db_does_not_consume_the_flag(monkeypatch):
    calls = []

    async def _fake_ensure_owner_profile(db=None):
        calls.append(db)
        return True

    monkeypatch.setattr(
        "modules.database.user_profiles.ensure_owner_profile",
        _fake_ensure_owner_profile,
    )

    orch_mod._maybe_seed_owner_profile(None)
    await asyncio.sleep(0)
    assert calls == []

    # A later call with a real db still gets scheduled — a no-db call must not
    # have consumed the once-flag.
    orch_mod._maybe_seed_owner_profile(object())
    await asyncio.sleep(0)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_seed_helper_never_raises_when_scheduling_fails(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(orch_mod.asyncio, "create_task", _boom)

    # Must not raise even though task scheduling itself is broken.
    orch_mod._maybe_seed_owner_profile(object())
