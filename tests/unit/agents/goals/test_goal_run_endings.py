"""116 W2.4 — every goal run gets ONE terminal `goal_run` row.

A run held by an owner pause, or cut by a crash, wrote board events only; only a
drained shutdown wrote `cut_by_restart` (by a hand loop in `_drain_goals`). Now
the hold writes the ending (`held` for a pause, `cut_by_restart` for a drained
shutdown, nothing for a rail yield, which writes `yielded`), a cancel no hold made
ends `cut`, and the boot requeue writes `cut_by_restart` for a crashed run.
"""
import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher


@pytest.fixture
def log(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    test_log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: test_log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    return test_log


def _endings(log, gid):
    return [r["attrs"]["outcome"] for r in log.query(kind="goal_run")
            if r["attrs"].get("goal_id") == gid and r["attrs"].get("outcome") != "started"]


def _setup(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="alice", title="Round 15", priority=5)
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "t.lock"))
    assert board.claim(g.id, disp._worker, ttl_seconds=900)
    return board, g, disp


@pytest.mark.asyncio
async def test_an_owner_pause_writes_held_once(tmp_path, log):
    board, g, disp = _setup(tmp_path)
    disp._goal_tasks[g.id] = asyncio.create_task(asyncio.sleep(3600))
    assert await disp.hold_inflight("owner pause (all)") == [g.id]
    assert _endings(log, g.id) == ["held"]
    assert disp._cut_kinds == {}


@pytest.mark.asyncio
async def test_a_drained_shutdown_writes_cut_by_restart_once(tmp_path, log):
    board, g, disp = _setup(tmp_path)
    disp._goal_tasks[g.id] = asyncio.create_task(asyncio.sleep(3600))
    held = await disp.hold_inflight("process shutdown (cut_by_restart)", reason_kind="restart")
    assert held == [g.id]
    assert _endings(log, g.id) == ["cut_by_restart"]
    # the row is back to ready: the next boot requeues nothing, so no second row
    assert board.requeue_running_on_boot_ids() == []


@pytest.mark.asyncio
async def test_a_rail_yield_writes_yielded_and_not_held(tmp_path, log):
    board, g, disp = _setup(tmp_path)
    disp._goal_tasks[g.id] = asyncio.create_task(asyncio.sleep(3600))
    disp._ws_holders.add(g.id)
    held = await disp.yield_for_rail(SimpleNamespace(id="exit0001", task="EXIT RAIL",
                                                     payload={"priority": "money"}))
    assert held == [g.id]
    assert _endings(log, g.id) == ["yielded"]


def test_a_crashed_run_gets_cut_by_restart_at_boot(tmp_path, log):
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="alice", title="Round 15")
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()                                       # the previous process is gone
    board.claim(g.id, f"goal-dispatch-{p.pid}", ttl_seconds=60)
    from polyrob.app.autonomy_runtime import _requeue_on_boot
    assert _requeue_on_boot(board) == 1
    assert _endings(log, g.id) == ["cut_by_restart"]
    rows = [r for r in log.query(kind="goal_run") if r["attrs"].get("goal_id") == g.id]
    assert rows[0]["user_id"] == "alice"


def test_cut_ending_is_silent_while_the_row_still_runs_under_this_claim(tmp_path, log):
    from agents.task.goals.goal_run import _GoalRunCtx
    from core.sqlite_util import execute_retry
    board, g, disp = _setup(tmp_path)
    live = board.get(g.id)
    disp._cut_ending(_GoalRunCtx(goal=live, hb_task=None))
    assert _endings(log, g.id) == []          # the drain / boot / reclaim ends it
    disp._cut_kinds[g.id] = "pause"
    execute_retry(board.db_path, "UPDATE goals SET status='ready' WHERE id=?", (g.id,))
    disp._cut_ending(_GoalRunCtx(goal=live, hb_task=None))
    assert _endings(log, g.id) == []          # a hold is making this cancel: it ends it
    disp._cut_kinds.clear()
    disp._cut_ending(_GoalRunCtx(goal=live, hb_task=None))
    assert _endings(log, g.id) == ["cut"]     # the owner moved the row: the run ends cut
