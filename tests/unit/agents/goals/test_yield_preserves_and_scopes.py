"""057 WS-C — a pre-emption must not discard work, and must not kill the planner.

Three defects the 057 survey found in the 056 WS5 yield (prod 2026-09-19: 17
yields in 24 h, one goal yielded 6 times):

1. ``attach_goal`` sat AFTER the ``wait_for``, so a cancelled run's artefacts
   were never attributed and the retry's ``_prior_artifacts`` found nothing.
2. ``hold_inflight`` cancelled EVERY task this process owned — including the
   planner, which is not a board row and so died silently.
3. A yield existed only in ``goals.db``; nothing in ``telemetry_events`` could
   count one, and the board event was called ``held_by_pause`` on a rail hold.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agents.task.goals.board import Goal, GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher
from core.artifacts import get_artifact_ledger


def _job(jid="exit0001", task="EXIT RAIL"):
    return SimpleNamespace(id=jid, task=task, payload={"priority": "money"})


class _HangingAgent:
    """create_session accepts a session_id (the real TaskAgent does); run_session
    never returns, so the run is still in flight when the rail pre-empts it."""

    def __init__(self):
        self.started = asyncio.Event()

    async def create_session(self, *, user_id, request, session_id=None, **kw):
        return {"id": session_id or "sess-hang"}

    async def run_session(self, user_id, session_id):
        self.started.set()
        await asyncio.sleep(3600)

    def get_orchestrator(self, session_id):
        return None


class _Board:
    """Minimal board: records nothing, so a recorded failure is visible."""

    def __init__(self):
        self.failures = []

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))
        return Goal(id=gid, user_id="rob", title="t", status="ready")

    def get(self, gid):
        return Goal(id=gid, user_id="rob", title="t", status="running")

    def stamp_block_kind(self, *a, **kw):
        return True

    def create_ask(self, **kw):
        return None


@pytest.mark.asyncio
async def test_a_cancelled_run_keeps_its_artefacts(tmp_path):
    """A1: the retry must be able to continue from what the pre-empted run wrote."""
    produced = tmp_path / "round15-notes.md"
    produced.write_text("half the report")
    agent = _HangingAgent()
    disp = GoalDispatcher(_Board(), agent)
    goal = Goal(id="g-yield", user_id="rob", title="Round 15",
                payload={"tools": ["filesystem"]})

    task = asyncio.create_task(disp._run_goal(goal))
    await asyncio.wait_for(agent.started.wait(), timeout=5)
    sid = disp._goal_sessions.get("g-yield")
    assert sid, "the dispatcher must know the session id before the run returns"
    get_artifact_ledger().record("rob", str(produced), session_id=sid, kind="report")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = get_artifact_ledger().list_for_goal("rob", "g-yield")
    assert [r.path for r in rows] == [str(produced)], \
        "a pre-empted run must keep the evidence it produced"


@pytest.mark.asyncio
async def test_a_rail_yield_never_cancels_the_planner(tmp_path):
    """A2: the planner is not a workspace writer and not a board row."""
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="rob", title="Round 15", body="do things", priority=5)
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "t.lock"))
    assert board.claim(g.id, disp._worker, ttl_seconds=900)

    planner_ran = asyncio.Event()

    async def _planner():
        try:
            await asyncio.sleep(0.2)
            planner_ran.set()
        except asyncio.CancelledError:
            raise

    ptask = asyncio.create_task(_planner())
    disp._inflight.add(ptask)

    goal_task = asyncio.create_task(asyncio.sleep(3600))
    disp._goal_tasks[g.id] = goal_task
    disp._ws_holders.add(g.id)

    held = await disp.yield_for_rail(_job())

    assert held == [g.id]
    assert goal_task.cancelled(), "the goal run holding the workspace is pre-empted"
    assert not ptask.cancelled(), "the planner must survive a rail yield"
    await asyncio.wait_for(planner_ran.wait(), timeout=5)
    disp._inflight.discard(ptask)


@pytest.mark.asyncio
async def test_an_owner_pause_still_holds_everything(tmp_path):
    """The pause shape is unchanged: planner included, rows back to ready."""
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="rob", title="Round 15", priority=5)
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "t.lock"))
    assert board.claim(g.id, disp._worker, ttl_seconds=900)
    ptask = asyncio.create_task(asyncio.sleep(3600))
    disp._inflight.add(ptask)

    held = await disp.hold_inflight("owner pause (all)")

    assert held == [g.id]
    assert ptask.cancelled(), "a pause stops the planner too"
    kinds = [e["kind"] for e in board.events(g.id)]
    assert "held" in kinds and "held_by_pause" in kinds
    payloads = {e["kind"]: e["payload"] for e in board.events(g.id)}
    assert payloads["held"]["reason_kind"] == "pause"


@pytest.mark.asyncio
async def test_a_rail_hold_is_labelled_rail_not_pause(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="rob", title="Round 15", priority=5)
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "t.lock"))
    assert board.claim(g.id, disp._worker, ttl_seconds=900)

    await disp.yield_for_rail(_job())

    ev = {e["kind"]: e["payload"] for e in board.events(g.id)}
    assert ev["held"]["reason_kind"] == "rail"
    assert "yielded" in ev
    assert board.yield_count(g.id) == 1


@pytest.mark.asyncio
async def test_a_yield_is_emitted_to_the_durable_event_log(tmp_path, monkeypatch):
    """A2: `/status work` cannot render `yielded xN` off goals.db alone."""
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    test_log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: test_log)

    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="rob", title="Round 15", priority=5)
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "t.lock"))
    assert board.claim(g.id, disp._worker, ttl_seconds=900)
    disp._goal_sessions[g.id] = "sess-15"

    await disp.yield_for_rail(_job())

    rows = [r for r in test_log.query(kind="goal_run")
            if r["attrs"].get("outcome") == "yielded"]
    assert len(rows) == 1
    attrs = rows[0]["attrs"]
    assert attrs["goal_id"] == g.id
    assert attrs["reason"] == "exit0001"
    assert attrs["job_task"] == "EXIT RAIL"
    assert rows[0]["session_id"] == "sess-15", \
        "the session id rides the event-log column, not attrs"


# --- B8: the grace period -----------------------------------------------------

@pytest.mark.asyncio
async def test_with_no_grace_the_run_is_cancelled_immediately(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="rob", title="Round 15", priority=5)
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "t.lock"))
    assert board.claim(g.id, disp._worker, ttl_seconds=900)
    task = asyncio.create_task(asyncio.sleep(3600))
    disp._goal_tasks[g.id] = task

    await disp.yield_for_rail(_job())

    assert task.cancelled(), "0 = today's behaviour, an immediate cancel"


@pytest.mark.asyncio
async def test_the_grace_asks_the_run_to_stop_at_its_step_boundary(tmp_path, monkeypatch):
    """B8: ending on a boundary saves the step's state; a hard cancel throws it away."""
    monkeypatch.setenv("GOAL_YIELD_GRACE_SEC", "5")
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="rob", title="Round 15", priority=5)

    asked = []
    orch = SimpleNamespace(cancel=lambda: asked.append(True), agents={})

    class _Agent:
        def get_orchestrator(self, sid):
            return orch

    disp = GoalDispatcher(board, _Agent(), lock_path=str(tmp_path / "t.lock"))
    assert board.claim(g.id, disp._worker, ttl_seconds=900)
    disp._goal_sessions[g.id] = "sess-15"

    finished = asyncio.Event()

    async def _cooperative():
        # The real run loop breaks out of its step loop on the cancel flag.
        while not asked:
            await asyncio.sleep(0.01)
        finished.set()

    task = asyncio.create_task(_cooperative())
    disp._goal_tasks[g.id] = task

    held = await disp.yield_for_rail(_job())

    assert asked == [True], "the run was ASKED to stop, not killed"
    assert finished.is_set() and not task.cancelled(), \
        "it ended on its own, inside the grace"
    assert held == [g.id]
    assert board.get(g.id).status == "ready"


@pytest.mark.asyncio
async def test_a_run_that_overruns_the_grace_is_still_cancelled(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_YIELD_GRACE_SEC", "1")
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="rob", title="Round 15", priority=5)
    orch = SimpleNamespace(cancel=lambda: None, agents={})

    class _Agent:
        def get_orchestrator(self, sid):
            return orch

    disp = GoalDispatcher(board, _Agent(), lock_path=str(tmp_path / "t.lock"))
    assert board.claim(g.id, disp._worker, ttl_seconds=900)
    disp._goal_sessions[g.id] = "sess-15"
    task = asyncio.create_task(asyncio.sleep(3600))
    disp._goal_tasks[g.id] = task

    held = await disp.yield_for_rail(_job())

    assert task.cancelled(), "the grace is a bound, not a promise"
    assert held == [g.id]
