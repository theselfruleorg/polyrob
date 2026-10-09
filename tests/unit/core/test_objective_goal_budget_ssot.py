"""OBJECTIVE_GOAL_BUDGET has ONE resolver (core.goal_vocab.objective_goal_budget):
the board enforces it and the status snapshot reports the same number."""

import pytest

from core import status_snapshot
from core.goal_vocab import objective_goal_budget


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OBJECTIVE_GOAL_BUDGET", raising=False)


@pytest.mark.parametrize("payload,want", [
    ({}, 25),
    ({"goal_budget": 4}, 4),
    ({"goal_budget": "7"}, 7),
    ({"goal_budget": -3}, 0),
    ({"goal_budget": "junk"}, 25),
    ({"stream_id": "s1", "goal_budget": 4}, 0),
    ({"recurrence": {"every": "1d"}}, 0),
    (None, 25),
])
def test_rule(payload, want):
    assert objective_goal_budget(payload) == want
    if payload is not None:
        assert status_snapshot._objective_budget(payload) == want


def test_env_default(monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "9")
    assert objective_goal_budget({}) == 9
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "x")
    assert objective_goal_budget({}) == 25


def test_board_uses_the_core_rule(monkeypatch):
    from types import SimpleNamespace
    from agents.task.goals.board import GoalBoard
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "6")
    for payload in ({}, {"goal_budget": 2}, {"recurrence": {"x": 1}}):
        obj = SimpleNamespace(payload=payload)
        assert GoalBoard.objective_budget(None, obj) == objective_goal_budget(payload)
