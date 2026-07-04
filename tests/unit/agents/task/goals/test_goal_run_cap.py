"""H11: a goal run had no wall-clock cap (only max_steps), so a single hung step
(tool/LLM/browser) blocked forever and permanently occupied a GOAL_MAX_CONCURRENT slot.
The run must be bounded by GOAL_MAX_RUN_SECONDS (like cron's per-job wait_for); a timeout
is recorded as a failure and the claim heartbeat is cancelled so the slot recovers.
"""
import asyncio

import pytest

from agents.task.goals.board import GoalBoard, STATUS_READY, STATUS_BLOCKED
from agents.task.goals import dispatcher as disp_mod
from agents.task.goals.dispatcher import GoalDispatcher


@pytest.mark.asyncio
async def test_goal_run_capped_by_max_run_seconds(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="u1", title="hangs", max_retries=5)
    assert board.claim(g.id, "w", ttl_seconds=900) is not None

    async def _hang(*a, **k):
        await asyncio.sleep(100)

    monkeypatch.setattr(disp_mod, "_run_task_as_session", _hang)
    monkeypatch.setattr(
        "agents.task.constants.AutonomyConfig.goal_max_run_seconds",
        staticmethod(lambda: 0.2),
    )

    class _Agent:
        pass

    d = GoalDispatcher(board, _Agent())

    # Must return promptly (~0.2s), not hang for 100s.
    await asyncio.wait_for(d._run_goal(g), timeout=5)

    got = board.get(g.id)
    assert got.consecutive_failures == 1  # timeout counted as a failure
    assert got.status in (STATUS_READY, STATUS_BLOCKED)
