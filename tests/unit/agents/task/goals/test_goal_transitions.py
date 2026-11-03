"""098 AG-17: goal status writes are guarded transitions."""
import asyncio

import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher
from agents.task.goals.machines import GOAL_MACHINE
from core.goal_owner_verbs import pause_goal, retry_goal
from core.state.machine import IllegalTransition


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_table_well_formed():
    assert GOAL_MACHINE.validate() == []


def test_retry_from_stale_blocked_snapshot_does_not_revive_cancelled(board):
    g = board.create(user_id="u", title="work")
    board.update_status(g.id, "blocked")
    snapshot = board.get(g.id)                 # Telegram /goals shows blocked
    assert board.cancel(g.id, user_id="u")     # REPL cancels
    res = retry_goal(board, user_id="u", goal=snapshot)
    assert res.ok is False and "changed" in res.message
    assert board.get(g.id).status == "cancelled"


def test_pause_running_clears_claim_token_so_old_run_cannot_record_success(board):
    g = board.create(user_id="u", title="work")
    run = board.claim(g.id, "w", ttl_seconds=60)
    res = pause_goal(board, user_id="u", goal=board.get(g.id))
    assert res.ok and "stops now" in res.message
    row = board.get(g.id)
    assert row.status == "paused" and row.claim_token is None and row.claim_lock is None
    assert board.record_success(g.id, result="late", claim_token=run.claim_token) is False
    assert board.get(g.id).status == "paused"


def test_pause_then_retry_cross_process_single_live_run(tmp_path):
    db = str(tmp_path / "goals.db")
    daemon, repl = GoalBoard(db), GoalBoard(db)
    g = daemon.create(user_id="u", title="work")
    first = daemon.claim(g.id, "goal-dispatch-1", ttl_seconds=600)
    assert pause_goal(repl, user_id="u", goal=repl.get(g.id)).ok
    # the daemon's run fails its next heartbeat BEFORE the REPL can re-claim
    assert daemon.heartbeat(g.id, "goal-dispatch-1", ttl_seconds=600,
                            claim_token=first.claim_token) is False
    assert retry_goal(repl, user_id="u", goal=repl.get(g.id)).ok
    second = repl.claim(g.id, "goal-dispatch-2", ttl_seconds=600)
    assert second is not None
    assert daemon.heartbeat(g.id, "goal-dispatch-1", ttl_seconds=600,
                            claim_token=first.claim_token) is False
    assert daemon.record_success(g.id, claim_token=first.claim_token) is False


@pytest.mark.asyncio
async def test_pause_cancels_the_local_run_at_once(board):
    g = board.create(user_id="u", title="work")
    board.claim(g.id, "w", ttl_seconds=60)
    d = GoalDispatcher(board, object())
    t = asyncio.create_task(asyncio.Event().wait())
    d._goal_tasks[g.id] = t
    assert pause_goal(board, user_id="u", goal=board.get(g.id)).ok
    await asyncio.sleep(0)
    assert t.cancelled() or t.cancelling()


def test_defer_run_preserves_concurrent_payload_key(board):
    g = board.create(user_id="u", title="work")
    run = board.claim(g.id, "w", ttl_seconds=60)
    board.merge_payload(g.id, {"other": "kept"})
    out = board.defer_run(g.id, retry_after_sec=60, claim_token=run.claim_token)
    assert out is not None
    p = board.get(g.id).payload
    assert p["other"] == "kept" and p["run_deferrals"] == 1 and p["not_before"] > 0


def test_defer_run_cap_is_in_the_guard(board):
    g = board.create(user_id="u", title="work", payload={"run_deferrals": 3})
    run = board.claim(g.id, "w", ttl_seconds=60)
    assert board.defer_run(g.id, retry_after_sec=60, claim_token=run.claim_token) is None
    assert board.get(g.id).status == "running"


def test_reclaim_stale_concurrent_does_not_double_count(tmp_path):
    db = str(tmp_path / "goals.db")
    clock = {"t": 1000.0}
    a = GoalBoard(db, clock=lambda: clock["t"])
    b = GoalBoard(db, clock=lambda: clock["t"])
    g = a.create(user_id="u", title="work", max_retries=5)
    a.claim(g.id, "w", ttl_seconds=10)
    clock["t"] += 60
    assert a.reclaim_stale() == 1
    assert b.reclaim_stale() == 0
    row = a.get(g.id)
    assert row.consecutive_failures == 1 and row.status == "ready"


def test_reclaim_skips_a_claim_refreshed_after_the_read(tmp_path, monkeypatch):
    db = str(tmp_path / "goals.db")
    clock = {"t": 1000.0}
    board = GoalBoard(db, clock=lambda: clock["t"])
    g = board.create(user_id="u", title="work")
    run = board.claim(g.id, "w", ttl_seconds=10)
    clock["t"] += 60
    import agents.task.goals.board as bm
    real = bm.execute_retry

    def heartbeat_lands(db_path, sql, params=(), **kw):
        out = real(db_path, sql, params, **kw)
        if sql.startswith("SELECT id, claim_lock, claim_expires"):
            real(db_path, "UPDATE goals SET claim_expires=? WHERE id=? AND status='running'",
                 (clock["t"] + 600, g.id))
        return out

    monkeypatch.setattr(bm, "execute_retry", heartbeat_lands)
    assert board.reclaim_stale() == 0
    monkeypatch.undo()
    row = board.get(g.id)
    assert row.status == "running" and row.consecutive_failures == 0
    assert run.claim_token == row.claim_token


def test_illegal_edge_raises_in_strict_mode(board):
    g = board.create(user_id="u", title="work")
    with pytest.raises(IllegalTransition):
        board.update_status(g.id, "done", from_states=("ready",))
