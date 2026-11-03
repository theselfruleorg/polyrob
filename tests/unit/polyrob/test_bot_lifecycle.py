"""098 CL-17 / CL-32: the CoreBot lifecycle."""
import asyncio

import pytest

from core.config import BotConfig
from polyrob.app import bot as bot_mod
from polyrob.app import initialization as init_mod


class _Log(list):
    phase = None


@pytest.fixture
def calls(monkeypatch):
    from core.container import DependencyContainer
    monkeypatch.setattr(DependencyContainer, "_instance", None)
    log = _Log()

    def phase(name, fail=False, hang=False):
        async def fn(container):
            log.append(name)
            if hang:
                await asyncio.sleep(3600)
            if fail:
                raise RuntimeError(f"{name} boom")
        return fn
    for n in ("initialize_core", "initialize_modules", "initialize_managers",
              "initialize_agents"):
        monkeypatch.setattr(bot_mod, n, phase(n))
    monkeypatch.setattr(bot_mod, "initialize_tools", phase("initialize_tools"))
    for n in ("cleanup_agents", "cleanup_managers", "cleanup_tools", "cleanup_modules",
              "cleanup_core"):
        monkeypatch.setattr(init_mod, n, phase(n))
    log.phase = phase
    return log


def _bot():
    return bot_mod.Bot(config=BotConfig())


def test_init_failure_runs_cleanup(calls, monkeypatch):
    async def go():
        monkeypatch.setattr(bot_mod, "initialize_tools", calls.phase("initialize_tools", fail=True))
        b = _bot()
        with pytest.raises(Exception):
            await b.initialize()
        assert "cleanup_modules" in calls and "cleanup_core" in calls
        assert not b._initialized
    asyncio.run(go())


def test_table_is_well_formed():
    assert bot_mod.BOT.validate() == []


def test_phase_timeout_reports_degraded(calls, monkeypatch):
    async def go():
        b = _bot()
        await b.start()
        assert b.state == "running" and b.is_running
        monkeypatch.setattr(init_mod, "cleanup_tools", calls.phase("cleanup_tools", hang=True))
        real_wait_for = asyncio.wait_for

        async def fast_wait_for(aw, timeout):
            return await real_wait_for(aw, min(timeout, 0.05))
        monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)
        await b.stop()
        assert b.state == "stopped_degraded" and not b.is_running
        assert b.cleanup_report["tools"] == "timeout"
        assert b.cleanup_report["core"] == "ok"  # later phases still ran
    asyncio.run(go())


def test_is_running_property_tracks_state(calls):
    async def go():
        b = _bot()
        assert b.state == "stopped" and not b.is_running
        await b.start()
        assert b.is_running and b.get_status()["state"] == "running"
        await b.stop()
        assert b.state == "stopped" and not b.is_running
        await b.start()  # stopped -> starting is legal again
        assert b.is_running
    asyncio.run(go())
