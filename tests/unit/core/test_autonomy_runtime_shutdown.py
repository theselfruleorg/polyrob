"""057 WS-H item 4: SIGTERM gives the in-flight work an ENDING.

Before this, `stop()` signalled the loop tasks and force-cancelled after
`_STOP_GRACE_SEC`, leaving the running cron/goal with a bare `started` in the
ledger and its row at `running` until the NEXT boot's orphan reclaim named it —
and three `stop-sigterm timed out -> Killing` in 7 d (prod, 2026-09-19) because
the grace window was waiting on an LLM call that would not answer inside it.
"""
import asyncio
import re
from pathlib import Path

import pytest

from core.autonomy_runtime import AutonomyHandles

REPO = Path(__file__).resolve().parents[3]


class FakeStore:
    def __init__(self):
        self.statuses = {}

    def set_status(self, job_id, status):
        self.statuses[job_id] = status


class FakeJob:
    id = "job-1"
    user_id = "u1"
    task = "EXIT rail"
    payload = {}


class FakeSched:
    """Mirrors the duck-typed surface `CronScheduler` really exposes."""
    events = []

    def __init__(self, task):
        self._current = task
        self._current_job = FakeJob()
        self.store = FakeStore()

    @staticmethod
    def _terminal_ev(job, outcome, reason=None, **extra):
        FakeSched.events.append((job.id, outcome, reason))


class FakeDispatcher:
    def __init__(self, held, *, hangs=False):
        self._held = held
        self.calls = []
        self._hangs = hangs
        self.board = type("B", (), {"get": staticmethod(
            lambda gid: type("G", (), {"user_id": "u1"})())})()

    async def hold_inflight(self, reason):
        self.calls.append(reason)
        if self._hangs:
            await asyncio.sleep(3600)
        return list(self._held)


def _handles(*, sched=None, dispatcher=None):
    h = AutonomyHandles()
    if sched is not None:
        h._loops["cron"] = type("T", (), {"scheduler": sched})()
    if dispatcher is not None:
        h._loops["goals"] = type("T", (), {"dispatcher": dispatcher})()
    return h


@pytest.fixture(autouse=True)
def _reset():
    FakeSched.events = []
    yield
    FakeSched.events = []


@pytest.mark.asyncio
async def test_inflight_cron_run_is_recorded_requeued_and_cancelled():
    started = asyncio.Event()

    async def never_answers():
        started.set()
        await asyncio.sleep(3600)          # the LLM call on the wire

    task = asyncio.create_task(never_answers())
    await started.wait()
    sched = FakeSched(task)
    h = _handles(sched=sched)

    await h.drain_inflight()
    await asyncio.sleep(0)

    assert FakeSched.events == [("job-1", "cut_by_restart", "process shutdown")]
    # back to `scheduled`, so the next boot's reclaim does NOT emit a SECOND
    # terminal event for the same run — one run, one ending.
    assert sched.store.statuses == {"job-1": "scheduled"}
    assert task.cancelled() or task.cancelling() or task.done()


@pytest.mark.asyncio
async def test_a_finished_cron_task_is_left_alone():
    async def done():
        return True
    task = asyncio.create_task(done())
    await task
    sched = FakeSched(task)
    await _handles(sched=sched).drain_inflight()
    assert FakeSched.events == []
    assert sched.store.statuses == {}


@pytest.mark.asyncio
async def test_goal_runs_are_held_and_each_gets_a_terminal_event(monkeypatch):
    recorded = []

    class FakeLog:
        def record(self, kind, **kw):
            recorded.append((kind, kw))

    monkeypatch.setattr("core.event_log.event_log_enabled", lambda: True)
    monkeypatch.setattr("core.event_log.get_event_log", lambda: FakeLog())
    disp = FakeDispatcher(["g1", "g2"])
    await _handles(dispatcher=disp).drain_inflight()

    assert disp.calls and "shutdown" in disp.calls[0]
    assert [k for k, _ in recorded] == ["goal_run", "goal_run"]
    outcomes = {kw["goal_id"]: kw["outcome"] for _, kw in recorded}
    assert outcomes == {"g1": "cut_by_restart", "g2": "cut_by_restart"}
    assert all(kw["user_id"] == "u1" for _, kw in recorded)


@pytest.mark.asyncio
async def test_a_hanging_hold_cannot_hang_shutdown():
    disp = FakeDispatcher([], hangs=True)
    import core.autonomy_runtime as ar
    grace = ar._STOP_GRACE_SEC
    ar._STOP_GRACE_SEC = 0.05
    try:
        await asyncio.wait_for(_handles(dispatcher=disp).drain_inflight(), timeout=5)
    finally:
        ar._STOP_GRACE_SEC = grace


@pytest.mark.asyncio
async def test_every_limb_is_fail_open():
    class Exploding:
        @property
        def _current(self):
            raise RuntimeError("boom")

    class ExplodingDispatcher:
        async def hold_inflight(self, reason):
            raise RuntimeError("boom")

    h = _handles(sched=Exploding(), dispatcher=ExplodingDispatcher())
    await h.drain_inflight()          # must not raise: shutdown proceeds


@pytest.mark.asyncio
async def test_the_flag_reverts_the_drain(monkeypatch):
    monkeypatch.setenv("AUTONOMY_SHUTDOWN_DRAIN", "off")
    task = asyncio.create_task(asyncio.sleep(3600))
    sched = FakeSched(task)
    disp = FakeDispatcher(["g1"])
    await _handles(sched=sched, dispatcher=disp).drain_inflight()
    assert FakeSched.events == [] and disp.calls == []
    task.cancel()


@pytest.mark.asyncio
async def test_stop_drains_before_it_tears_the_loops_down():
    order = []

    class Ticker:
        async def run_forever(self, stop_event=None):
            try:
                await stop_event.wait()
            finally:
                order.append("loop_stopped")

    h = AutonomyHandles()
    h._add("cron", Ticker())

    async def drain():
        order.append("drained")
    h.drain_inflight = drain
    await h.stop()
    assert order == ["drained", "loop_stopped"]


@pytest.mark.asyncio
async def test_no_loops_is_a_no_op():
    await AutonomyHandles().drain_inflight()


def test_the_unit_allows_the_drain_to_finish():
    unit = REPO / "deployment/polyrob.service"
    # `deployment/` is owner-infra and does not ship in the public package, where a
    # bare `pytest` still collects this file. An absent file is 'not this
    # tree's concern', never a failure.
    if not unit.exists():
        pytest.skip("deployment sources absent (public package)")
    svc = unit.read_text()
    m = re.search(r"^TimeoutStopSec=(\d+)\s*$", svc, re.M)
    assert m and int(m.group(1)) >= 90, "a 60s stop timeout forced 3 kills in 7 d"


def test_the_deploy_reports_an_unclean_shutdown():
    script = REPO / "scripts/deploy_prod.sh"
    # `scripts/` is owner-infra and does not ship in the public package, where a
    # bare `pytest` still collects this file. An absent file is 'not this
    # tree's concern', never a failure.
    if not script.exists():
        pytest.skip("deploy scripts absent (public package)")
    sh = script.read_text()
    assert "stop-sigterm timed out" in sh
    assert "QUIESCE_TS=" in sh, "the window must start BEFORE deploy_stop_units"
    # Non-fatal: the code being deployed is not what timed out.
    assert "WARN: the previous instance did NOT stop cleanly" in sh


# --- the drain is duck-typed; pin the real shapes it reaches for -----------
# `core` may not import `cron.*`/`agents.*`, so the drain reads attributes by
# name. A rename there would turn it into a silent no-op — the failure mode a
# fail-open helper hides best. These cases hold the contract from the outside.

def test_the_real_cron_scheduler_exposes_what_the_drain_reads():
    from cron.scheduler import CronScheduler
    for attr in ("_current", "_current_job", "_terminal_ev", "cancel_inflight"):
        assert hasattr(CronScheduler, attr) or attr in CronScheduler.__init__.__code__.co_names, attr
    from cron.jobs import CronJobStore
    assert callable(getattr(CronJobStore, "set_status", None))


def test_the_real_goal_dispatcher_exposes_what_the_drain_reads():
    from agents.task.goals.dispatcher import GoalDispatcher
    assert callable(getattr(GoalDispatcher, "hold_inflight", None))
    from agents.task.goals.board import GoalBoard
    assert callable(getattr(GoalBoard, "get", None))
    assert callable(getattr(GoalBoard, "hold_running", None))


def test_cut_by_restart_is_the_vocabulary_the_ledger_already_renders():
    """056 WS1 named this outcome; the shutdown drain must use THAT word, not a
    second one nobody renders."""
    src = (REPO / "cron/jobs.py").read_text()
    assert '"cut_by_restart"' in src
    status = (REPO / "core/status_snapshot.py").read_text()
    assert "cut_by_restart" in status
