"""The planner's numeric limits scale with the number of standing streams."""
import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals.planner import build_planner_prompt, planner_ceilings


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_ceiling_derives_from_the_objective_count(monkeypatch):
    monkeypatch.delenv("GOAL_PLANNER_READY_CEILING", raising=False)
    assert planner_ceilings(3)["ready_ceiling"] == 5      # floor of 5
    assert planner_ceilings(6)["ready_ceiling"] == 6
    assert planner_ceilings(16)["ready_ceiling"] == 16


def test_owner_can_pin_the_ceiling(monkeypatch):
    monkeypatch.setenv("GOAL_PLANNER_READY_CEILING", "8")
    assert planner_ceilings(16)["ready_ceiling"] == 8


def test_social_and_per_run_defaults(monkeypatch):
    monkeypatch.delenv("GOAL_PLANNER_MAX_SOCIAL", raising=False)
    monkeypatch.delenv("GOAL_PLANNER_GOALS_PER_RUN", raising=False)
    c = planner_ceilings(10)
    assert c["social"] == 1 and c["per_run"] == 3
    monkeypatch.setenv("GOAL_PLANNER_MAX_SOCIAL", "3")
    assert planner_ceilings(10)["social"] == 3


def test_prompt_uses_the_derived_numbers(board, monkeypatch):
    monkeypatch.delenv("GOAL_PLANNER_READY_CEILING", raising=False)
    for i in range(12):
        board.create_objective(user_id="u1", title=f"stream {i}", force=True)
    prompt = build_planner_prompt(board, "u1", None)
    assert "Never exceed 12 ready goals total" in prompt
    assert "At most 1 goal may include 'twitter'" in prompt
    assert "Create 1-3 goals with goal_create" in prompt


@pytest.mark.asyncio
async def test_planner_fires_on_a_wide_board_that_the_old_gate_ignored(board, monkeypatch):
    """5 ready goals across 10 streams is a THIN board, not a healthy one."""
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_PLANNER_ENABLED", "true")
    monkeypatch.delenv("GOAL_PLANNER_READY_CEILING", raising=False)
    monkeypatch.setenv("GOAL_PLANNER_COOLDOWN_SEC", "0")

    from agents.task.goals.dispatcher import GoalDispatcher

    objs = [board.create_objective(user_id="u1", title=f"stream {i}", force=True)
            for i in range(10)]
    for o in objs[:5]:
        board.create(user_id="u1", title=f"g-{o.id[:4]}", parent_id=o.id, force=True)

    planned = []

    class _Agent:
        pass

    # MUST be a coroutine function: `_maybe_plan` wraps the call in
    # `asyncio.create_task`, and `create_task(None)` raises TypeError.
    async def _fake_planner(uid):
        planned.append(uid)

    d = GoalDispatcher(board, _Agent())
    monkeypatch.setattr(d, "_run_planner", _fake_planner)
    await d._maybe_plan(headroom_after=0)
    import asyncio
    await asyncio.sleep(0)   # let the created task run one turn
    assert planned == ["u1"]


# --- GOAL_PLANNER_SCALING: one revert for the planner half (spec R8) ---------


def test_scaling_off_restores_the_pre_scaling_ceiling(monkeypatch):
    """`GOAL_PLANNER_READY_CEILING` cannot serve as the revert: the derived value
    is a `max()` of it, and pinning it to 1 corrupts the prompt to "Never exceed
    1 ready goals total"."""
    monkeypatch.delenv("GOAL_PLANNER_READY_CEILING", raising=False)
    monkeypatch.setenv("GOAL_PLANNER_SCALING", "false")
    assert planner_ceilings(16)["ready_ceiling"] == 5
    assert planner_ceilings(3)["ready_ceiling"] == 5


def test_scaling_off_restores_the_pre_scaling_prompt(board, monkeypatch):
    monkeypatch.delenv("GOAL_PLANNER_READY_CEILING", raising=False)
    monkeypatch.setenv("GOAL_PLANNER_SCALING", "false")
    for i in range(12):
        board.create_objective(user_id="u1", title=f"stream {i}", force=True)
    prompt = build_planner_prompt(board, "u1", None)
    assert "Never exceed 5 ready goals total" in prompt
    assert "SERVE THESE OBJECTIVES FIRST" not in prompt


def test_scaling_on_is_the_default(board, monkeypatch):
    monkeypatch.delenv("GOAL_PLANNER_SCALING", raising=False)
    monkeypatch.delenv("GOAL_PLANNER_READY_CEILING", raising=False)
    for i in range(12):
        board.create_objective(user_id="u1", title=f"stream {i}", force=True)
    prompt = build_planner_prompt(board, "u1", None)
    assert "Never exceed 12 ready goals total" in prompt
    assert "SERVE THESE OBJECTIVES FIRST" in prompt


def test_pinned_ceiling_still_wins_when_scaling_is_off(monkeypatch):
    monkeypatch.setenv("GOAL_PLANNER_SCALING", "false")
    monkeypatch.setenv("GOAL_PLANNER_READY_CEILING", "8")
    assert planner_ceilings(16)["ready_ceiling"] == 8
