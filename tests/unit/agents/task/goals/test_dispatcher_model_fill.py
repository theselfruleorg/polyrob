"""Autonomous goal runs must resolve a concrete model from the registry when the goal
payload doesn't pin one. Regression: a None model crashed session setup downstream with
AttributeError 'NoneType' object has no attribute 'lower', so every headless goal failed."""
from unittest.mock import patch

import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals import dispatcher as disp_mod
from agents.task.goals.dispatcher import GoalDispatcher


class _DummyAgent:
    deliver_self_wake = None  # _self_wake no-ops when absent/None


@pytest.mark.asyncio
async def test_run_goal_fills_default_model_when_payload_has_none(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    goal = board.create(user_id="rob", title="t", body="do x",
                        payload={"tools": ["filesystem"]})  # no model pinned

    captured = {}

    async def _fake_run(task_agent, *, user_id, request, autonomous=False):
        captured.update(request)
        from agents.task.runtime.run_outcome import RunOutcome
        return RunOutcome(session_id="sess-1", status="ok")

    # get_default_model is imported lazily inside _run_goal; patch it at its source.
    with patch.object(disp_mod, "_run_task_to_outcome", _fake_run), \
         patch("modules.llm.llm_client_registry.get_default_model", return_value="prov/model-x"):
        d = GoalDispatcher(board, _DummyAgent())
        await d._run_goal(goal)

    assert captured.get("model"), "model must be filled, not None"
    assert captured["model"] == "prov/model-x"
