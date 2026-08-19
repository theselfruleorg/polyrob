"""The planner is shown each objective's SPEND, not just its description.

Without it the planner cannot tell a fresh objective from one that has already
spawned 32 children over a month, so it opens the next round either way.
"""
from pathlib import Path

from agents.task.goals.board import GoalBoard
from agents.task.goals.planner import build_planner_prompt


def _board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_a_fresh_objective_shows_headroom(tmp_path, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "10")
    b = _board(tmp_path)
    b.create_objective(user_id="rob", title="Explore the x402 economy")

    prompt = build_planner_prompt(b, "rob", None)

    assert "goal budget: 0/10 live" in prompt
    assert "SPENT" not in prompt


def test_a_spent_objective_is_told_to_stop_opening_rounds(tmp_path, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "2")
    b = _board(tmp_path)
    obj = b.create_objective(user_id="rob", title="Explore the x402 economy")
    for i in range(2):
        b.create(user_id="rob", title=f"round {i}", parent_id=obj.id, force=True)

    prompt = build_planner_prompt(b, "rob", None)

    assert "goal budget: 2/2 live" in prompt
    assert "SPENT" in prompt
    assert "raise an ask" in prompt
