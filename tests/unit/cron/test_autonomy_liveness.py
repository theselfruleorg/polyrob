"""Task 17 — autonomy liveness: planner in the local safe group, self-wake
strong ref, reclaim-before-gate (AU-F1.1 / AU-F3.1 / AU-F4.3).
"""
import asyncio
import types

import pytest

from agents.task.constants import AutonomyConfig


# ---------------------------------------------------------------------------
# AU-F1.1 — GOAL_PLANNER_ENABLED joins the POLYROB_LOCAL safe group.
# ---------------------------------------------------------------------------


def test_goal_planner_enabled_defaults_on_under_local(monkeypatch):
    monkeypatch.delenv("GOAL_PLANNER_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert AutonomyConfig.goal_planner_enabled() is True


def test_goal_planner_enabled_defaults_off_without_local(monkeypatch):
    monkeypatch.delenv("GOAL_PLANNER_ENABLED", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    assert AutonomyConfig.goal_planner_enabled() is False


def test_goal_planner_enabled_explicit_value_wins_over_local(monkeypatch):
    """An explicit per-flag value still wins -- only the default moves."""
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("GOAL_PLANNER_ENABLED", "false")
    assert AutonomyConfig.goal_planner_enabled() is False


# ---------------------------------------------------------------------------
# AU-F3.1 — deliver_self_wake's run_session task is strongly referenced.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_self_wake_holds_strong_ref_to_run_session_task(monkeypatch):
    """The asyncio.create_task(...) fired at the end of a successful self-wake
    dispatch must be retained in a module-level strong-ref set (mirrors
    core/autonomy_runtime.py's _BACKGROUND_TASKS) -- otherwise asyncio's weak
    reference to a fire-and-forget task lets it be GC'd mid-run."""
    from agents.task.agent.core.self_wake import reset_reentry_budget
    from agents.task_agent_lite import TaskAgent, _SELF_WAKE_TASKS

    monkeypatch.setenv("SELF_WAKE_ENABLED", "true")
    monkeypatch.setenv("SELF_WAKE_MAX_REENTRIES", "5")
    monkeypatch.setenv("SELF_WAKE_IDLE_BACKOFF_SEC", "0")
    reset_reentry_budget()

    agent = object.__new__(TaskAgent)  # no __init__: only wire what's needed
    agent.session_manager = types.SimpleNamespace(
        get_session_info=lambda sid: {"id": sid}
    )

    class _FakeOrch:
        async def submit_user_message(self, **kwargs):
            return None

    async def _resolve_or_recreate(session_id, session_info):
        return _FakeOrch()

    agent._resolve_or_recreate = _resolve_or_recreate

    ran = asyncio.Event()

    async def _run_session(user_id, session_id):
        ran.set()

    agent.run_session = _run_session

    before = len(_SELF_WAKE_TASKS)
    try:
        result = await agent.deliver_self_wake("sess", "user", "wake up")
        assert result is True
        # The task must be retained synchronously before it gets a chance to run.
        assert len(_SELF_WAKE_TASKS) == before + 1

        await asyncio.wait_for(ran.wait(), timeout=1)
        await asyncio.sleep(0)  # let the done-callback fire
        assert len(_SELF_WAKE_TASKS) == before  # discarded on completion
    finally:
        reset_reentry_budget()


# ---------------------------------------------------------------------------
# AU-F4.3 — reclaim_stale runs even when GOALS_ENABLED is off.
# ---------------------------------------------------------------------------


class _FakeAgent:
    async def create_session(self, *, user_id, request):
        return {"id": f"sess-{user_id}"}

    async def run_session(self, user_id, session_id):
        return "result"

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        return True

    def get_orchestrator(self, session_id):
        return None


@pytest.mark.asyncio
async def test_reclaim_stale_called_even_when_goals_disabled(tmp_path, monkeypatch):
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.dispatcher import GoalDispatcher

    monkeypatch.setenv("GOALS_ENABLED", "false")

    board = GoalBoard(str(tmp_path / "goals.db"))
    calls = []
    monkeypatch.setattr(board, "reclaim_stale", lambda: calls.append(1))

    d = GoalDispatcher(board, _FakeAgent())
    n = await d.dispatch_once()

    assert n == 0  # dispatch itself still gated off
    assert calls == [1]  # but reclaim ran regardless
