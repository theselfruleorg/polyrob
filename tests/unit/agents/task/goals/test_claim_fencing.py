"""A reclaimed goal belongs to its new execution, including same-worker retries."""
import asyncio
import sqlite3

import pytest

from modules.goals.board import GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher


@pytest.mark.parametrize("late_result", ["success", "failure"])
@pytest.mark.parametrize("replacement_worker", ["old-worker", "new-worker"])
def test_late_result_cannot_overwrite_replacement(tmp_path, late_result, replacement_worker):
    clock = [1000.0]
    board = GoalBoard(str(tmp_path / "goals.db"), clock=lambda: clock[0])
    goal = board.create(user_id="u", title="prepare output", max_retries=3)
    child = board.create(user_id="u", title="consume output", depends_on=[goal.id])
    old = board.claim(goal.id, "old-worker", ttl_seconds=30)
    board.stamp_session(goal.id, "old-session")
    clock[0] = 1031.0
    assert board.reclaim_stale() == 1
    new = board.claim(goal.id, replacement_worker, ttl_seconds=30)
    assert new.claim_token != old.claim_token
    board.stamp_session(goal.id, "new-session")
    if late_result == "success":
        assert board.record_success(goal.id, session_id="old-session", result="old result",
                                    claim_token=old.claim_token) is False
    else:
        board.record_failure(goal.id, session_id="old-session", error="old failure",
                             claim_token=old.claim_token)
    current = board.get(goal.id)
    assert (current.status, current.claim_lock, current.session_id) == (
        "running", replacement_worker, "new-session")
    assert current.consecutive_failures == 1
    assert board.get(child.id).status == "waiting"
    assert not board.heartbeat(goal.id, "old-worker", ttl_seconds=90,
                               claim_token=old.claim_token)
    assert board.record_success(goal.id, result="new result", claim_token=new.claim_token)
    assert board.get(child.id).status == "ready"


def test_token_cannot_be_omitted_for_a_new_claim(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    goal = board.create(user_id="u", title="protected work")
    claimed = board.claim(goal.id, "worker", ttl_seconds=60)
    assert not board.record_success(goal.id, result="unfenced success")
    board.record_failure(goal.id, error="unfenced failure")
    assert board.get(goal.id).claim_token == claimed.claim_token
    assert board.get(goal.id).status == "running"
    assert board.get(goal.id).consecutive_failures == 0


def test_existing_database_migrates_without_replacing_claims(tmp_path):
    path = str(tmp_path / "goals.db")
    board = GoalBoard(path)
    goal = board.create(user_id="u", title="existing work")
    board.claim(goal.id, "worker", ttl_seconds=60)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE goals DROP COLUMN claim_token")
    migrated = GoalBoard(path)
    assert migrated.get(goal.id).status == "running"
    assert migrated.get(goal.id).claim_lock == "worker"
    assert migrated.get(goal.id).claim_token is None
    assert migrated.record_success(goal.id, result="legacy completion")


@pytest.mark.asyncio
async def test_heartbeat_cancels_run_after_losing_claim(tmp_path, monkeypatch):
    board = GoalBoard(str(tmp_path / "goals.db"))
    goal = board.create(user_id="u", title="work")
    old = board.claim(goal.id, "worker", ttl_seconds=60)
    board.update_status(goal.id, "ready")
    new = board.claim(goal.id, "worker", ttl_seconds=60)
    dispatcher = GoalDispatcher(board, object())
    monkeypatch.setattr("agents.task.goals.dispatcher._heartbeat_interval", lambda ttl: 0)
    started = asyncio.Event()

    async def run():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await started.wait()
    await dispatcher._heartbeat_claim(goal.id, "worker", 60,
                                       claim_token=old.claim_token, run_task=task)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert board.get(goal.id).claim_token == new.claim_token
    assert board.get(goal.id).status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_dispatcher_does_not_publish_a_late_result(tmp_path, monkeypatch, failed):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("core.runtime_config.resolve_live_provider", lambda _: "openai")
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")
    clock = [1000.0]
    board = GoalBoard(str(tmp_path / "goals.db"), clock=lambda: clock[0])
    goal = board.create(user_id="u", title="required output", max_retries=3,
                        payload={"tools": ["filesystem"], "model": "test"})
    old = board.claim(goal.id, "old-worker", ttl_seconds=30)

    class Agent:
        async def create_session(self, *, user_id, request, **kwargs):
            clock[0] = 1031.0
            board.reclaim_stale()
            board.claim(goal.id, "new-worker", ttl_seconds=30)
            board.stamp_session(goal.id, "new-session")
            return {"id": "old-session"}

        async def run_session(self, user_id, session_id):
            if failed:
                raise RuntimeError("old attempt failed")
            return "old attempt finished\nOUTCOME: old output"

        def get_orchestrator(self, sid):
            return None

    dispatcher = GoalDispatcher(board, Agent())
    dispatcher._notify_owner_done = AsyncMock()
    dispatcher._self_wake = AsyncMock()
    dispatcher._maybe_escalate_blocked = AsyncMock()
    await dispatcher._run_goal(old)
    current = board.get(goal.id)
    assert (current.status, current.claim_lock, current.session_id) == (
        "running", "new-worker", "new-session")
    assert "outcome" not in current.payload
    dispatcher._notify_owner_done.assert_not_awaited()
    dispatcher._self_wake.assert_not_awaited()
    dispatcher._maybe_escalate_blocked.assert_not_awaited()


def test_old_task_callback_does_not_remove_replacement(tmp_path):
    dispatcher = GoalDispatcher(GoalBoard(str(tmp_path / "goals.db")), object())
    old, new = object(), object()
    dispatcher._goal_tasks["g"] = new
    dispatcher._ws_holders.add("g")
    dispatcher._forget_goal_task("g", task=old)
    assert dispatcher._goal_tasks["g"] is new
    assert "g" in dispatcher._ws_holders


@pytest.mark.asyncio
async def test_heartbeat_does_not_cancel_its_own_completion(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("core.runtime_config.resolve_live_provider", lambda _: "openai")
    monkeypatch.setattr("agents.task.goals.dispatcher._heartbeat_interval", lambda ttl: 0)
    monkeypatch.setattr("agents.task.goals.dispatcher.effective_goal_notify_on_done",
                        lambda *args: True)
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")
    board = GoalBoard(str(tmp_path / "goals.db"))
    goal = board.create(user_id="u", title="finish normally",
                        payload={"tools": ["filesystem"], "model": "test"})
    claimed = board.claim(goal.id, "worker", ttl_seconds=60)

    class Agent:
        async def create_session(self, *, user_id, request, **kwargs):
            return {"id": "audit-session"}

        async def run_session(self, user_id, session_id):
            return "finished the required output"

        def get_orchestrator(self, sid):
            return None

    async def notify(*args, **kwargs):
        await asyncio.sleep(0)  # the old heartbeat could run during delivery

    dispatcher = GoalDispatcher(board, Agent())
    dispatcher._notify_owner_done = AsyncMock(side_effect=notify)
    await dispatcher._run_goal(claimed)
    assert board.get(goal.id).status == "done"
    dispatcher._notify_owner_done.assert_awaited_once()


def test_a_reclaimed_claim_records_no_failure_and_returns_none(tmp_path):
    """125 S16 (B-H9): the stale path returns None, never the CURRENT row — a
    caller that compared tokens on the current row could act for a newer claim."""
    clock = [1000.0]
    board = GoalBoard(str(tmp_path / "goals.db"), clock=lambda: clock[0])
    goal = board.create(user_id="u", title="t", max_retries=3)
    old = board.claim(goal.id, "w", ttl_seconds=30)
    clock[0] = 1031.0
    assert board.reclaim_stale() == 1
    before = board.get(goal.id).consecutive_failures
    assert board.record_failure(goal.id, error="late", claim_token=old.claim_token) is None
    assert board.get(goal.id).consecutive_failures == before


@pytest.mark.asyncio
async def test_a_heartbeat_that_cannot_renew_cancels_the_run(tmp_path, monkeypatch):
    """125 S31 (B-L5): a heartbeat that keeps raising lets the claim expire; another
    worker then reclaims the goal and runs it twice. Once the claim cannot have
    been renewed before its TTL, the run is cancelled (the "lost its claim" path)."""
    import agents.task.goals.dispatcher as disp_mod
    board = GoalBoard(str(tmp_path / "goals.db"))
    d = GoalDispatcher(board, task_agent=object())
    now = [1000.0]
    monkeypatch.setattr(disp_mod, "_heartbeat_interval", lambda ttl: 0.01)
    monkeypatch.setattr(disp_mod, "_hb_clock", lambda: now[0])

    def _raise(*a, **k):
        now[0] += 25.0   # each failed beat costs time on the claim
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(board, "heartbeat", _raise)

    run = asyncio.create_task(asyncio.sleep(60))
    hb = asyncio.create_task(d._heartbeat_claim("g1", "w", 60, claim_token="t", run_task=run))
    await asyncio.wait_for(hb, 2.0)
    await asyncio.sleep(0)
    assert run.cancelled()


@pytest.mark.asyncio
async def test_one_failed_heartbeat_does_not_cancel_the_run(tmp_path, monkeypatch):
    import agents.task.goals.dispatcher as disp_mod
    board = GoalBoard(str(tmp_path / "goals.db"))
    d = GoalDispatcher(board, task_agent=object())
    monkeypatch.setattr(disp_mod, "_heartbeat_interval", lambda ttl: 0.01)
    monkeypatch.setattr(disp_mod, "_hb_clock", lambda: 1000.0)
    calls = []

    def _raise_once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return True
    monkeypatch.setattr(board, "heartbeat", _raise_once)
    run = asyncio.create_task(asyncio.sleep(60))
    hb = asyncio.create_task(d._heartbeat_claim("g1", "w", 60, claim_token="t", run_task=run))
    await asyncio.sleep(0.1)
    assert not run.done()
    hb.cancel(); run.cancel()
