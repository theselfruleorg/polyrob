import asyncio
import shutil

import pytest
import core.autonomy_runtime as ar


@pytest.fixture(autouse=True)
def _reset_owner_profile_seed_flag():
    """G-1: isolate the once-per-process owner-profile-seed flag between tests
    in this file so the seeding tests below are deterministic regardless of
    run order (other tests in this file call start_autonomy too)."""
    ar._owner_profile_seed_scheduled = False
    ar._boot_migrations_scheduled = True  # keep unrelated tests migration-free
    yield
    ar._owner_profile_seed_scheduled = False
    ar._boot_migrations_scheduled = False


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
async def test_surface_gc_tick_purges_expired_correspondents(monkeypatch):
    """A7 (2026-07-13 review): with CORRESPONDENT_TTL_DAYS set, the GC tick calls
    CorrespondentRegistry.purge_expired (it previously had no production caller)."""
    monkeypatch.setenv("CORRESPONDENT_TTL_DAYS", "30")
    calls = {}

    class _ChatReg:
        def purge_stale(self, older_than_secs):
            return 0

    class _Corr:
        def purge_expired(self, ttl_secs):
            calls["ttl"] = ttl_secs
            return 2

    class _Container:
        def get_service(self, name):
            return {"session_chat_registry": _ChatReg(),
                    "correspondent_registry": _Corr()}.get(name)

    task_agent = type("TA", (), {"container": _Container()})()
    ticker = ar._build_surface_gc_ticker(task_agent)
    await ticker.tick_coro()
    assert calls["ttl"] == 30 * 86400


@pytest.mark.asyncio
async def test_surface_gc_tick_skips_correspondent_purge_by_default(monkeypatch):
    """Default TTL 0 = never expire (expiry breaks reply routing; explicit opt-in)."""
    monkeypatch.delenv("CORRESPONDENT_TTL_DAYS", raising=False)
    calls = {}

    class _ChatReg:
        def purge_stale(self, older_than_secs):
            return 0

    class _Corr:
        def purge_expired(self, ttl_secs):
            calls["ttl"] = ttl_secs
            return 0

    class _Container:
        def get_service(self, name):
            return {"session_chat_registry": _ChatReg(),
                    "correspondent_registry": _Corr()}.get(name)

    task_agent = type("TA", (), {"container": _Container()})()
    ticker = ar._build_surface_gc_ticker(task_agent)
    await ticker.tick_coro()
    assert calls == {}


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
    monkeypatch.setattr(ar, "_quiet_release_enabled", lambda: False)


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
    monkeypatch.setattr(ar, "_quiet_release_enabled", lambda: False)

    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.01)

    names = [name for name, _task, _stop in handles._entries]
    assert sorted(names) == ["cron", "curator", "goals", "surface_gc"]
    assert "orphan_reap" not in names and "docker_reap" not in names

    await handles.stop()


@pytest.mark.asyncio
async def test_start_autonomy_starts_quiet_release_ticker_when_enabled(monkeypatch):
    # 018 P0.3: the defer-to-window-end sweep rides the autonomy runtime.
    ticker = _FakeTicker()
    monkeypatch.setattr(ar, "_build_quiet_release_ticker", lambda ta: ticker)
    monkeypatch.setattr(ar, "_quiet_release_enabled", lambda: True)
    monkeypatch.setattr(ar, "_cron_enabled", lambda: False)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: False)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: False)
    monkeypatch.setattr(ar, "_surface_gc_enabled", lambda: False)
    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.01)
    assert ticker.started
    await handles.stop()
    assert ticker.stopped


def test_quiet_release_enabled_follows_rail_and_prefs(monkeypatch):
    # Gate = user-delivery rail ON (its default) AND prefs ON (its default).
    monkeypatch.delenv("SEND_MESSAGE_USER_DELIVERY", raising=False)
    monkeypatch.delenv("PREFS_ENABLED", raising=False)
    assert ar._quiet_release_enabled() is True
    monkeypatch.setenv("SEND_MESSAGE_USER_DELIVERY", "false")
    assert ar._quiet_release_enabled() is False
    monkeypatch.delenv("SEND_MESSAGE_USER_DELIVERY", raising=False)
    monkeypatch.setenv("PREFS_ENABLED", "off")
    assert ar._quiet_release_enabled() is False


@pytest.mark.asyncio
async def test_start_autonomy_starts_settlement_watcher_when_enabled(monkeypatch):
    watcher = _FakeTicker()
    monkeypatch.setattr(ar, "_build_settlement_watcher", lambda ta: watcher)
    monkeypatch.setattr(ar, "_x402_invoicing_enabled", lambda: True)
    monkeypatch.setattr(ar, "_cron_enabled", lambda: False)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: False)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: False)
    monkeypatch.setattr(ar, "_surface_gc_enabled", lambda: False)
    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.01)
    assert watcher.started
    await handles.stop()
    assert watcher.stopped


@pytest.mark.asyncio
async def test_start_autonomy_settlement_watcher_off_by_default(monkeypatch):
    monkeypatch.delenv("X402_INVOICE_ENABLED", raising=False)
    monkeypatch.setattr(ar, "_cron_enabled", lambda: False)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: False)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: False)
    monkeypatch.setattr(ar, "_surface_gc_enabled", lambda: False)
    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    names = [n for n, _, _ in handles._entries]
    assert "settlement" not in names
    await handles.stop()


# --------------------------------------------------------------------------
# G-1 (metering finalization) — start_autonomy seeds the owner/local
# user_profiles row(s) once per process so FK-constrained metering writes
# (usage_records -> user_profiles) don't raise IntegrityError on a headless
# deployment where nothing else seeds user_profiles.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_autonomy_schedules_owner_profile_seed_once(monkeypatch):
    _disable_other_loops(monkeypatch)
    calls = []

    async def _fake_ensure(db=None):
        calls.append(db)
        return True

    monkeypatch.setattr("modules.database.user_profiles.ensure_owner_profile", _fake_ensure)

    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    handles2 = ar.start_autonomy(task_agent=object(), data_dir="data")  # 2nd call: no-op (once-flag)
    await asyncio.sleep(0.01)

    assert len(calls) == 1
    await handles.stop()
    await handles2.stop()


@pytest.mark.asyncio
async def test_owner_profile_seed_uses_task_agent_container_database(monkeypatch):
    _disable_other_loops(monkeypatch)
    calls = []

    async def _fake_ensure(db=None):
        calls.append(db)
        return True

    monkeypatch.setattr("modules.database.user_profiles.ensure_owner_profile", _fake_ensure)

    sentinel_db = object()

    class _Container:
        def get_service(self, name):
            return sentinel_db if name == "database_manager" else None

    task_agent = type("TA", (), {"container": _Container()})()

    handles = ar.start_autonomy(task_agent=task_agent, data_dir="data")
    await asyncio.sleep(0.01)

    assert calls == [sentinel_db]
    await handles.stop()


@pytest.mark.asyncio
async def test_owner_profile_seed_scheduling_failure_is_fail_open(monkeypatch):
    _disable_other_loops(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(ar.asyncio, "create_task", _boom)

    # Must not raise even though task scheduling itself is broken.
    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await handles.stop()


# --------------------------------------------------------------------------
# D2 (2026-07-14 review) — boot migrations ran ONLY in the API lifespan, so the
# headless/CLI postures (polyrob telegram, chat REPL, email/gateway) never
# migrated bot.db. start_autonomy now schedules the same fail-open,
# snapshot-first run_boot_migrations once per process for every posture.
# --------------------------------------------------------------------------

class _MigContainer:
    def get_service(self, name):
        return None


@pytest.mark.asyncio
async def test_start_autonomy_schedules_boot_migrations_once(monkeypatch):
    _disable_other_loops(monkeypatch)
    ar._boot_migrations_scheduled = False
    calls = []

    async def _fake_boot(container, *, local=True):
        calls.append((container, local))
        return {"applied": [], "error": None}

    monkeypatch.setattr("migrations.boot.run_boot_migrations", _fake_boot)

    container = _MigContainer()
    task_agent = type("TA", (), {"container": container})()
    handles = ar.start_autonomy(task_agent=task_agent, data_dir="data")
    handles2 = ar.start_autonomy(task_agent=task_agent, data_dir="data")  # once-flag
    await asyncio.sleep(0.01)

    assert calls == [(container, True)]
    await handles.stop()
    await handles2.stop()


@pytest.mark.asyncio
async def test_boot_migrations_failure_is_fail_open(monkeypatch):
    _disable_other_loops(monkeypatch)
    ar._boot_migrations_scheduled = False

    async def _boom(container, *, local=True):
        raise RuntimeError("migration wiring exploded")

    monkeypatch.setattr("migrations.boot.run_boot_migrations", _boom)

    task_agent = type("TA", (), {"container": _MigContainer()})()
    handles = ar.start_autonomy(task_agent=task_agent, data_dir="data")
    await asyncio.sleep(0.01)  # the scheduled task must swallow the error
    await handles.stop()


# --------------------------------------------------------------------------
# 013 T2 review, Finding 2: `_x402_invoicing_enabled` read raw env only, so
# the settlement watcher never started under autonomous mode even though the
# `x402_invoice` tool (tools/x402/__init__.py) was already wired to allow
# invoice creation — invoices creatable but never settleable. Fixed with the
# same guarded-OR pattern, applied LOCALLY (core/autonomy_runtime.py cannot
# import modules.x402 — that would put a server-tier module on the core
# import graph, the C3 boundary — so it can't share the SSOT directly).
# --------------------------------------------------------------------------

def _enable_full(monkeypatch):
    """Copied from tests/unit/agents/task/test_autonomy_mode.py."""
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    from agents.task import constants
    constants.reset_autonomy_mode_warnings()


def test_x402_invoicing_enabled_off_supervised_default(monkeypatch):
    """(a) supervised/unset -> disabled exactly as today."""
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("X402_INVOICE_ENABLED", raising=False)
    assert ar._x402_invoicing_enabled() is False


def test_x402_invoicing_enabled_on_under_autonomous_mode(monkeypatch):
    """(b) effective autonomous mode -> default flips ON."""
    _enable_full(monkeypatch)
    monkeypatch.delenv("X402_INVOICE_ENABLED", raising=False)
    assert ar._x402_invoicing_enabled() is True


def test_x402_invoicing_enabled_explicit_false_wins_over_mode(monkeypatch):
    """(c) explicit env false wins over the mode default."""
    _enable_full(monkeypatch)
    monkeypatch.setenv("X402_INVOICE_ENABLED", "false")
    assert ar._x402_invoicing_enabled() is False


@pytest.mark.asyncio
async def test_start_autonomy_starts_settlement_watcher_under_autonomous_mode_default(monkeypatch):
    """Integration at the actual start_autonomy consumer seam: under autonomous
    mode with X402_INVOICE_ENABLED unset, the settlement watcher now starts
    (previously stayed off — this file's own `_x402_invoicing_enabled` never
    saw the mode default)."""
    _enable_full(monkeypatch)
    monkeypatch.delenv("X402_INVOICE_ENABLED", raising=False)
    watcher = _FakeTicker()
    monkeypatch.setattr(ar, "_build_settlement_watcher", lambda ta: watcher)
    _disable_other_loops(monkeypatch)
    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    await asyncio.sleep(0.01)
    assert watcher.started
    await handles.stop()
    assert watcher.stopped


@pytest.mark.asyncio
async def test_boot_migrations_skipped_without_container(monkeypatch):
    _disable_other_loops(monkeypatch)
    ar._boot_migrations_scheduled = False
    calls = []

    async def _fake_boot(container, *, local=True):
        calls.append(container)
        return {}

    monkeypatch.setattr("migrations.boot.run_boot_migrations", _fake_boot)

    handles = ar.start_autonomy(task_agent=object(), data_dir="data")  # no .container
    await asyncio.sleep(0.01)
    assert calls == []
    await handles.stop()
