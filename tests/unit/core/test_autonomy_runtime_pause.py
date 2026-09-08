"""031 T6: the pause transition reconciles THIS process; cold-start sweeps honour it."""
import asyncio

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    for k in ("AUTONOMY_HALT", "DATA_ROOT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_transition_hook_holds_dispatcher_and_cancels_cron(home):
    from core import autonomy_control as ac
    from core.autonomy_runtime import AutonomyHandles
    calls = []

    class FakeDispatcher:
        _paused_seen = False

        async def hold_inflight(self, reason):
            calls.append(("hold", reason))
            return ["g1"]

    class FakeSched:
        def cancel_inflight(self):
            # the real one is kind-aware: nothing to cancel when cron_run is allowed
            from core.autonomy_control import allows
            if allows("cron_run").allowed:
                return False
            calls.append(("cron", None))
            return True

    h = AutonomyHandles()
    h._loops = {"goals": type("T", (), {"dispatcher": FakeDispatcher()})(),
                "cron": type("T", (), {"scheduler": FakeSched()})()}
    ac.register_transition_hook(h.on_pause_transition)
    try:
        ac.pause(str(home), via="test")
        await asyncio.sleep(0.05)
        # a resume edge does nothing; a second identical pause does nothing
        ac.resume(str(home))
        ac.pause(str(home), scopes=("streams",), via="test")
        await asyncio.sleep(0.05)
    finally:
        ac._TRANSITION_HOOKS.clear()
    assert ("cron", None) in calls and any(c[0] == "hold" for c in calls)
    assert h._loops["goals"].dispatcher._paused_seen is True
    # the streams-only pause must NOT hold dispatch/cron (their kinds are not covered)
    assert calls.count(("cron", None)) == 1


@pytest.mark.asyncio
async def test_transition_hook_cancels_autonomous_session_delegations(home):
    from core import autonomy_control as ac
    from core.autonomy_runtime import AutonomyHandles
    from agents.task.goals.autonomy_marker import mark_autonomous

    class Reg:
        def __init__(self):
            self.cancelled = []

        def cancel_all(self, reason):
            self.cancelled.append(reason)
            return 2

    auto_reg, chat_reg = Reg(), Reg()
    orchs = {"auto-sid": type("O", (), {"async_delegation": auto_reg})(),
             "chat-sid": type("O", (), {"async_delegation": chat_reg})()}

    class Registry:
        def session_ids(self):
            return list(orchs)

        def get(self, sid):
            return orchs.get(sid)

    mark_autonomous("auto-sid")
    import logging
    from agents.task.task_agent_lifecycle import TaskAgentLifecycleMixin as _LM
    agent = type("A", (), {"_registry": Registry(), "logger": logging.getLogger("t"),
                           "is_autonomous_session": _LM.is_autonomous_session,
                           "cancel_autonomous_delegations": _LM.cancel_autonomous_delegations})()
    h = AutonomyHandles()
    h._task_agent = agent
    ac.register_transition_hook(h.on_pause_transition)
    try:
        ac.pause(str(home), via="test")
        await asyncio.sleep(0.05)
    finally:
        ac._TRANSITION_HOOKS.clear()
    assert auto_reg.cancelled == ["owner pause"]
    assert chat_reg.cancelled == []  # the owner's own chat session is never touched


def test_cold_start_requeue_runs_even_while_paused(home):
    """A `running` row after a restart is a lie whatever the pause state; the
    requeue starts nothing (dispatch is gated) and keeps `reclaim_stale` from
    counting the restart as a failure later."""
    import subprocess
    import sys

    from core import autonomy_control as ac
    from agents.task.goals.board import GoalBoard
    b = GoalBoard(str(home / "goals.db"))
    g = b.create(user_id="rob", title="a")
    # The claim of the previous, now-dead process (the sweep's subject). The claim
    # format matters since the ownership guard added to `requeue_running_on_boot`
    # requeues only a row whose owner is expired or provably gone — this test is
    # about the PAUSE not gating the sweep, not about ripping a live claim.
    _p = subprocess.Popen([sys.executable, "-c", "pass"])
    _p.wait()
    b.claim(g.id, f"goal-dispatch-{_p.pid}", ttl_seconds=60)
    ac.pause(str(home), via="test")
    from core.autonomy_runtime import _requeue_on_boot
    assert _requeue_on_boot(b) == 1
    assert b.get(g.id).status == "ready" and b.get(g.id).consecutive_failures == 0
    assert ac.allows("dispatch", str(home)).allowed is False


@pytest.mark.asyncio
async def test_pause_watcher_reconciles_a_pause_written_by_another_process(home, monkeypatch):
    """The CLI / web console / a script / a touched file write the record from
    ANOTHER process: no transition hook fires here, so the watcher must see the
    edge and cancel this process's in-flight work."""
    from core import autonomy_control as ac
    from core.autonomy_runtime import AutonomyHandles
    calls = []

    class FakeDispatcher:
        _paused_seen = False

        async def hold_inflight(self, reason):
            calls.append(("hold", reason))
            return ["g1"]

    h = AutonomyHandles()
    h._loops = {"goals": type("T", (), {"dispatcher": FakeDispatcher()})()}
    h._PAUSE_WATCH_SEC = 0.05
    h._start_pause_watcher(str(home))
    try:
        await asyncio.sleep(0.1)
        (home / "AUTONOMY_HALT").write_text("")  # a `touch` from outside this process
        for _ in range(40):
            await asyncio.sleep(0.05)
            if calls:
                break
    finally:
        await h.stop()
    assert calls and calls[0][0] == "hold"


def test_start_autonomy_registers_the_hook_and_logs_a_paused_start(home, monkeypatch, caplog):
    import logging
    from core import autonomy_control as ac
    from core import autonomy_runtime as ar
    for name in ("_cron_enabled", "_goals_enabled", "_curator_enabled", "_surface_gc_enabled",
                 "_quiet_release_enabled", "_x402_invoicing_enabled"):
        monkeypatch.setattr(ar, name, lambda: False)
    monkeypatch.setattr(ar, "_schedule_boot_migrations", lambda ta: None)
    monkeypatch.setattr(ar, "_schedule_owner_profile_seed", lambda ta: None)
    monkeypatch.setattr(ar, "_schedule_cold_start_orphan_reap", lambda: None)
    monkeypatch.setattr(ar, "_schedule_delegation_recovery", lambda ta, dd=None: None)
    monkeypatch.setattr(ar, "_schedule_hf_deploy_reconcile", lambda: None)
    ac.pause(str(home), via="test")
    try:
        with caplog.at_level(logging.WARNING, logger="core.autonomy_runtime"):
            async def _run():
                h = ar.start_autonomy(task_agent=object(), data_dir=str(home))
                registered = h.on_pause_transition in ac._TRANSITION_HOOKS
                watching = h._watch_task is not None
                await h.stop()  # unregisters the hook + stops the watcher
                return h, registered, watching
            h, registered, watching = asyncio.run(_run())
        assert registered and watching
        assert h.on_pause_transition not in ac._TRANSITION_HOOKS
        assert any("PAUSED" in r.getMessage() and "ARMED" in r.getMessage() for r in caplog.records)
    finally:
        ac._TRANSITION_HOOKS.clear()
