"""056 WS5 — a money rail pre-empts a running board goal (D1, owner 'go all').

Prod 2026-09-19: board goals held the hourly EXIT rail 33, 30, 20 and then 60+
minutes in one night (`GOAL_MAX_RUN_SECONDS` was 4 h). The headroom rule bounds
a goal's START; nothing bounded a goal already running. Now a cron job carrying
`payload.priority == "money"` that comes due while the process is busy BECAUSE OF
A GOAL (no human turn marker) makes the scheduler call the dispatcher's
`yield_for_rail`: the goal's task is cancelled at its step boundary, its board
row returns to `ready` with no failure increment and a `resume_note`, and the
rail runs on the same tick. Gated `GOAL_YIELD_FOR_MONEY_RAIL` (default OFF).
"""
import asyncio
from datetime import datetime

import pytest

from cron.jobs import CronJob, CronJobStore
from cron.scheduler import CronScheduler

NOW = datetime(2026, 9, 19, 6, 0, 0)


def _job(**kw):
    base = dict(id="exit0001", task="EXIT RAIL", schedule_spec="0 * * * *", user_id="rob",
                next_run_at=datetime(2026, 9, 19, 5, 0, 0), max_duration_seconds=60,
                payload={"priority": "money"})
    base.update(kw)
    return CronJob(**base)


def test_is_money_job_reads_payload_priority():
    from cron.jobs import is_money_job
    assert is_money_job(_job()) is True
    assert is_money_job(_job(payload={})) is False
    assert is_money_job(_job(payload={"priority": "ops"})) is False


@pytest.mark.asyncio
async def test_money_job_yields_a_running_goal_then_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_YIELD_FOR_MONEY_RAIL", "true")
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / "locks"))
    from core import interactive_gate as g
    g._busy_depth = 0
    g.mark_busy()  # a goal run holds the process (no human turn marker)
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job())
    ran = []
    yielded = []

    async def runner(job):
        ran.append(job.id)
        return True

    async def yield_hook(job):
        yielded.append(job.id)
        g.mark_idle()  # the dispatcher cancelled the goal → process idle
        return ["goal-a"]

    sched = CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock"))
    sched.set_yield_hook(yield_hook)
    res = await sched.tick(now=NOW)
    assert yielded == ["exit0001"] and ran == ["exit0001"]
    assert not res.skipped_busy
    g._busy_depth = 0


@pytest.mark.asyncio
async def test_ops_job_does_not_yield_and_still_skips_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_YIELD_FOR_MONEY_RAIL", "true")
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / "locks"))
    from core import interactive_gate as g
    g._busy_depth = 0
    g.mark_busy()
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job(payload={}))
    yielded = []

    async def runner(job):
        return True

    async def yield_hook(job):
        yielded.append(job.id)
        return []

    sched = CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock"))
    sched.set_yield_hook(yield_hook)
    res = await sched.tick(now=NOW)
    assert res.skipped_busy and yielded == []
    g._busy_depth = 0


@pytest.mark.asyncio
async def test_a_human_turn_is_never_yielded(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_YIELD_FOR_MONEY_RAIL", "true")
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / "locks"))
    from core import interactive_gate as g
    g._busy_depth = 0
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job())
    yielded = []

    async def runner(job):
        return True

    async def yield_hook(job):
        yielded.append(job.id)
        return []

    sched = CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock"))
    sched.set_yield_hook(yield_hook)
    with g.owner_turn(kind="owner_chat", session_id="s1"):
        res = await sched.tick(now=NOW)
    assert res.skipped_busy and yielded == [], "the owner's turn outranks a money rail"


@pytest.mark.asyncio
async def test_dispatcher_yield_for_rail_returns_row_to_ready_with_note(tmp_path, monkeypatch):
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.dispatcher import GoalDispatcher
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="rob", title="Round 15", body="do things", priority=5)
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "goals.tick.lock"))
    claimed = board.claim(g.id, disp._worker, ttl_seconds=900)
    assert claimed
    held = await disp.yield_for_rail(_job())
    assert held == [g.id]
    row = board.get(g.id)
    assert row.status == "ready" and row.consecutive_failures == 0
    assert "EXIT RAIL" in (row.payload or {}).get("resume_note", "")
    kinds = [e["kind"] for e in board.events(g.id)] if hasattr(board, "events") else []
    if kinds:
        assert "yielded" in kinds
