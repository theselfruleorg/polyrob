"""§5.1-5.4 durable goal stewardship — framework = substrate, agent = steward.

- §5.1 cold-start sweep: a goal `running` across a deploy is re-queued
  immediately WITHOUT a consecutive_failures increment (a restart is not the
  goal's fault; mirrors cron's reclaim of running rows).
- §5.2 attempt ledger: retries stop being amnesiac — each failure appends
  {ts, error, session_id} to payload.attempts, the retry prompt carries a
  compact previous-attempt block, and goal_show exposes the history.
- §5.3 blocked stewardship: an `unblock` verb (symmetric to fulfill_ask) +
  ancient blocked goals age out VISIBLY (→ cancelled, logged), never rotting
  as permanent planner context.
- §5.4 quota exhaustion pauses runs, not curation (planning still fires).
"""
import asyncio
import time

import pytest

from agents.task.goals.board import (GoalBoard, STATUS_BLOCKED, STATUS_CANCELLED,
                                     STATUS_READY, STATUS_RUNNING)


@pytest.fixture()
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


# ---------------------------------------------------------------------------
# §5.1 cold-start sweep
# ---------------------------------------------------------------------------

def test_cold_start_sweep_requeues_running_without_failure_increment(board):
    g = board.create(user_id="u1", title="mid-flight goal")
    assert board.claim(g.id, "w1", ttl_seconds=900) is not None
    assert board.get(g.id).status == STATUS_RUNNING

    n = board.requeue_running_on_boot()
    assert n == 1
    got = board.get(g.id)
    assert got.status == STATUS_READY
    assert got.consecutive_failures == 0, \
        "a process restart is NOT the goal's failure (two deploys must not block it)"
    assert got.claim_lock is None


def test_cold_start_sweep_ignores_non_running(board):
    board.create(user_id="u1", title="ready goal")
    assert board.requeue_running_on_boot() == 0


def test_autonomy_runtime_runs_the_sweep(tmp_path, monkeypatch):
    """start_autonomy must sweep the goal board at boot (mirrors its existing
    cold-start sweeps for docker orphans + delegations)."""
    import inspect
    import core.autonomy_runtime as rt
    src = inspect.getsource(rt)
    assert "requeue_running_on_boot" in src


# ---------------------------------------------------------------------------
# §5.2 attempt ledger + retry context
# ---------------------------------------------------------------------------

def test_record_failure_appends_attempt_to_payload(board):
    g = board.create(user_id="u1", title="retry me", max_retries=3)
    assert board.claim(g.id, "w1", ttl_seconds=900)
    board.record_failure(g.id, error="x402 store unavailable", session_id="s1")
    got = board.get(g.id)
    attempts = (got.payload or {}).get("attempts") or []
    assert attempts and attempts[-1]["error"].startswith("x402 store unavailable")
    assert attempts[-1]["session_id"] == "s1"
    assert "ts" in attempts[-1]


def test_attempts_are_capped(board):
    g = board.create(user_id="u1", title="flaky", max_retries=100)
    for i in range(8):
        assert board.claim(g.id, "w1", ttl_seconds=900)
        board.record_failure(g.id, error=f"failure {i}", session_id=f"s{i}")
    attempts = (board.get(g.id).payload or {}).get("attempts") or []
    assert len(attempts) <= 5, "the ledger is a compact tail, not an unbounded log"
    assert attempts[-1]["error"] == "failure 7"


def test_retry_prompt_carries_previous_attempt_block(board):
    from agents.task.goals.context import build_goal_run_task
    g = board.create(user_id="u1", title="retry me", body="do the thing", max_retries=3)
    assert board.claim(g.id, "w1", ttl_seconds=900)
    board.record_failure(g.id, error="completion judge: no successful post action",
                         session_id="s1")
    got = board.get(g.id)
    prompt = build_goal_run_task(got, None)
    assert "PREVIOUS ATTEMPT" in prompt
    assert "no successful post action" in prompt


def test_first_attempt_prompt_has_no_previous_attempt_block(board):
    from agents.task.goals.context import build_goal_run_task
    g = board.create(user_id="u1", title="fresh goal", body="do it")
    assert "PREVIOUS ATTEMPT" not in build_goal_run_task(g, None)


def test_goal_show_exposes_attempts_and_acceptance(board):
    from tools.goal_tools import GoalTool, GoalShowAction
    g = board.create(user_id="u1", title="steward me",
                     payload={"acceptance": "a live url"})
    assert board.claim(g.id, "w1", ttl_seconds=900)
    board.record_failure(g.id, error="judge: unmet — nothing posted", session_id="s1")

    tool = GoalTool.__new__(GoalTool)
    tool._resolve_board = lambda: board
    tool._user = lambda ec: "u1"
    res = asyncio.run(GoalTool.goal_show(tool, GoalShowAction(goal_id=g.id)))
    text = res.extracted_content or ""
    assert "unmet — nothing posted" in text, "the agent must SEE its attempt history"
    assert "a live url" in text, "acceptance is part of the goal's visible contract"


# ---------------------------------------------------------------------------
# §5.3 blocked stewardship: unblock verb + visible aging
# ---------------------------------------------------------------------------

def test_unblock_requeues_with_rationale(board):
    g = board.create(user_id="u1", title="blocked goal", max_retries=1)
    assert board.claim(g.id, "w1", ttl_seconds=900)
    board.record_failure(g.id, error="boom", session_id="s1")  # trips breaker (max_retries=1)
    assert board.get(g.id).status == STATUS_BLOCKED

    ok = board.unblock(g.id, user_id="u1", rationale="credentials granted")
    assert ok is True
    got = board.get(g.id)
    assert got.status == STATUS_READY
    assert got.consecutive_failures == 0


def test_unblock_refuses_wrong_tenant_and_non_blocked(board):
    g = board.create(user_id="u1", title="ready goal")
    assert board.unblock(g.id, user_id="u1", rationale="x") is False  # not blocked
    g2 = board.create(user_id="u1", title="blocked goal 2", max_retries=1)
    assert board.claim(g2.id, "w1", ttl_seconds=900)
    board.record_failure(g2.id, error="boom")
    assert board.unblock(g2.id, user_id="OTHER", rationale="x") is False  # tenant scope


def test_ancient_blocked_goals_age_out_to_cancelled(board):
    import sqlite3
    g = board.create(user_id="u1", title="rotting goal", max_retries=1)
    assert board.claim(g.id, "w1", ttl_seconds=900)
    board.record_failure(g.id, error="boom")
    assert board.get(g.id).status == STATUS_BLOCKED
    # age the row far past the window (timestamps are float epochs)
    old = time.time() - 40 * 86400
    conn = sqlite3.connect(board.db_path)
    conn.execute("UPDATE goals SET completed_at=?, created_at=? WHERE id=?", (old, old, g.id))
    conn.commit()
    conn.close()

    n = board.age_out_blocked(max_age_days=14)
    assert n == 1
    assert board.get(g.id).status == STATUS_CANCELLED


def test_recent_blocked_goals_do_not_age_out(board):
    g = board.create(user_id="u1", title="fresh blocked", max_retries=1)
    assert board.claim(g.id, "w1", ttl_seconds=900)
    board.record_failure(g.id, error="boom")
    assert board.age_out_blocked(max_age_days=14) == 0
    assert board.get(g.id).status == STATUS_BLOCKED


# ---------------------------------------------------------------------------
# §5.4 quota exhaustion pauses runs, not curation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quota_exhaustion_still_plans(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "1")

    g = board.create(user_id="u1", title="already ran")
    assert board.claim(g.id, "w", ttl_seconds=900)
    board.record_success(g.id, result="done")  # 1 started in 24h -> quota exhausted

    from agents.task.goals.dispatcher import GoalDispatcher

    class _Agent:
        pass

    d = GoalDispatcher(board, _Agent())
    planned = []

    async def _plan(*, headroom_after):
        planned.append(headroom_after)

    monkeypatch.setattr(d, "_maybe_plan", _plan)
    n = await d.dispatch_once()
    assert n == 0, "quota pauses RUNS"
    assert planned, "…but NOT curation/planning (§5.4)"


def test_goal_unblock_tool_verb_owner_only(board):
    """The unblock verb is agent-visible but autonomy-refused — the agent
    proposes, the owner (interactive session) executes."""
    from tools.goal_tools import GoalTool, GoalUnblockAction
    g = board.create(user_id="u1", title="blocked", max_retries=1)
    assert board.claim(g.id, "w1", ttl_seconds=900)
    board.record_failure(g.id, error="boom")
    assert board.get(g.id).status == STATUS_BLOCKED

    tool = GoalTool.__new__(GoalTool)
    tool._resolve_board = lambda: board
    tool._user = lambda ec: "u1"
    res = asyncio.run(GoalTool.goal_unblock(
        tool, GoalUnblockAction(goal_id=g.id, rationale="credentials granted")))
    assert not res.error
    assert board.get(g.id).status == STATUS_READY
