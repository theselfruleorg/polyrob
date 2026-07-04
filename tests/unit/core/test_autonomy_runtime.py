import asyncio
import shutil

import pytest
import core.autonomy_runtime as ar


class _FakeTicker:
    def __init__(self):
        self.started = False; self.stopped = False
    async def run_forever(self, *, stop_event):
        self.started = True
        await stop_event.wait()
        self.stopped = True


@pytest.mark.asyncio
async def test_start_autonomy_starts_enabled_loops(monkeypatch):
    cron, goal, cur = _FakeTicker(), _FakeTicker(), _FakeTicker()
    monkeypatch.setattr(ar, "_build_cron_ticker", lambda ta, data_dir: cron)
    monkeypatch.setattr(ar, "_build_goal_ticker", lambda ta, data_dir: goal)
    monkeypatch.setattr(ar, "_build_curator_ticker", lambda data_dir: cur)
    monkeypatch.setattr(ar, "_cron_enabled", lambda: True)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: True)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: True)
    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.01)
    assert cron.started and goal.started and cur.started
    await handles.stop()
    assert cron.stopped and goal.stopped and cur.stopped


@pytest.mark.asyncio
async def test_start_autonomy_skips_disabled(monkeypatch):
    cron = _FakeTicker()
    monkeypatch.setattr(ar, "_build_cron_ticker", lambda ta, data_dir: cron)
    monkeypatch.setattr(ar, "_cron_enabled", lambda: False)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: False)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: False)
    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.01)
    assert cron.started is False
    await handles.stop()


@pytest.mark.asyncio
async def test_one_loop_failing_does_not_block_others(monkeypatch):
    goal = _FakeTicker()
    def _boom(ta, data_dir): raise RuntimeError("cron build failed")
    monkeypatch.setattr(ar, "_build_cron_ticker", _boom)
    monkeypatch.setattr(ar, "_build_goal_ticker", lambda ta, data_dir: goal)
    monkeypatch.setattr(ar, "_cron_enabled", lambda: True)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: True)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: False)
    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.01)
    assert goal.started
    await handles.stop()


@pytest.mark.asyncio
async def test_start_autonomy_starts_surface_gc_when_enabled(monkeypatch):
    """a5: when SURFACE_GC_ENABLED, start_autonomy spins the chat-binding GC loop."""
    gc = _FakeTicker()
    monkeypatch.setattr(ar, "_build_surface_gc_ticker", lambda ta: gc)
    monkeypatch.setattr(ar, "_surface_gc_enabled", lambda: True)
    monkeypatch.setattr(ar, "_cron_enabled", lambda: False)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: False)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: False)
    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.01)
    assert gc.started
    await handles.stop()
    assert gc.stopped


@pytest.mark.asyncio
async def test_surface_gc_tick_purges_stale_bindings(monkeypatch):
    """The GC tick resolves the registry from the task_agent container and purges with
    the configured horizon. Fail-open: no registry -> no error."""
    from agents.task.surface_config import SurfaceConfig

    purged = {}
    class _Reg:
        def purge_stale(self, older_than_secs):
            purged["horizon"] = older_than_secs
            return 3
    class _Container:
        def get_service(self, name):
            return _Reg() if name == "session_chat_registry" else None
    task_agent = type("TA", (), {"container": _Container()})()

    ticker = ar._build_surface_gc_ticker(task_agent)
    await ticker.tick_coro()
    assert purged["horizon"] == SurfaceConfig.surface_gc_horizon_secs()


@pytest.mark.asyncio
async def test_surface_gc_tick_fail_open_without_registry():
    task_agent = type("TA", (), {"container": None})()
    ticker = ar._build_surface_gc_ticker(task_agent)
    await ticker.tick_coro()  # must not raise


@pytest.mark.asyncio
async def test_stop_force_cancels_stubborn_ticker(monkeypatch):
    monkeypatch.setattr(ar, "_STOP_GRACE_SEC", 0.05)

    class _StubbornTicker:
        def __init__(self): self.cancelled = False
        async def run_forever(self, *, stop_event):
            try:
                await asyncio.sleep(100)   # ignores stop_event entirely
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    stubborn = _StubbornTicker()
    monkeypatch.setattr(ar, "_build_goal_ticker", lambda ta, data_dir: stubborn)
    monkeypatch.setattr(ar, "_cron_enabled", lambda: False)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: True)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: False)
    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.01)
    await handles.stop()           # must return promptly, not hang ~100s
    assert stubborn.cancelled is True


# --------------------------------------------------------------------------
# P1-B review Important #2 — cold-start-only orphan sandbox-container reap.
#
# `DockerBackend.reap_orphans()` must run exactly ONCE, at process start, and
# NEVER on a periodic ticker (a periodic sweep would force-kill containers
# belonging to sessions that are simply idle between turns, not orphaned).
# --------------------------------------------------------------------------

def _disable_other_loops(monkeypatch):
    monkeypatch.setattr(ar, "_cron_enabled", lambda: False)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: False)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: False)
    monkeypatch.setattr(ar, "_surface_gc_enabled", lambda: False)


def _install_reap_spy(monkeypatch):
    calls = []

    async def _fake_reap_orphans(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    from tools.code_exec.backends.docker import DockerBackend
    monkeypatch.setattr(DockerBackend, "reap_orphans", staticmethod(_fake_reap_orphans))
    return calls


@pytest.mark.asyncio
async def test_start_autonomy_schedules_one_cold_start_orphan_reap_when_enabled(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    _disable_other_loops(monkeypatch)
    calls = _install_reap_spy(monkeypatch)

    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.05)  # let the fire-and-forget sweep task run

    assert len(calls) == 1
    # not wired as a recurring ticker — no extra entry in the handles' loop list
    assert handles._entries == []
    await handles.stop()


@pytest.mark.asyncio
async def test_start_autonomy_skips_orphan_reap_when_flag_off(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_DOCKER_PERSISTENT", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    _disable_other_loops(monkeypatch)
    calls = _install_reap_spy(monkeypatch)

    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.05)

    assert calls == []
    await handles.stop()


@pytest.mark.asyncio
async def test_start_autonomy_skips_orphan_reap_when_docker_cli_missing(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _disable_other_loops(monkeypatch)
    calls = _install_reap_spy(monkeypatch)

    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.05)

    assert calls == []
    await handles.stop()


@pytest.mark.asyncio
async def test_start_autonomy_orphan_reap_failure_is_fail_open(monkeypatch):
    """A crashing reap_orphans() must never take down start_autonomy or the
    other loops — swallow + log only."""
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    _disable_other_loops(monkeypatch)

    async def _boom(*args, **kwargs):
        raise RuntimeError("docker ps exploded")

    from tools.code_exec.backends.docker import DockerBackend
    monkeypatch.setattr(DockerBackend, "reap_orphans", staticmethod(_boom))

    handles = ar.start_autonomy(task_agent=object(), data_dir="data")  # must not raise
    await asyncio.sleep(0.05)
    await handles.stop()


@pytest.mark.asyncio
async def test_start_autonomy_orphan_reap_never_added_to_recurring_entries(monkeypatch):
    """Regression guard for 'Do NOT add it to any recurring ticker': even with
    every OTHER loop also enabled, the orphan sweep must not appear as a
    stoppable/recurring entry in AutonomyHandles."""
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    _install_reap_spy(monkeypatch)

    cron, goal, cur, gc = _FakeTicker(), _FakeTicker(), _FakeTicker(), _FakeTicker()
    monkeypatch.setattr(ar, "_build_cron_ticker", lambda ta, data_dir: cron)
    monkeypatch.setattr(ar, "_build_goal_ticker", lambda ta, data_dir: goal)
    monkeypatch.setattr(ar, "_build_curator_ticker", lambda data_dir: cur)
    monkeypatch.setattr(ar, "_build_surface_gc_ticker", lambda ta: gc)
    monkeypatch.setattr(ar, "_cron_enabled", lambda: True)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: True)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: True)
    monkeypatch.setattr(ar, "_surface_gc_enabled", lambda: True)

    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.01)

    names = [name for name, _task, _stop in handles._entries]
    assert sorted(names) == ["cron", "curator", "goals", "surface_gc"]
    assert "orphan_reap" not in names and "docker_reap" not in names

    await handles.stop()
