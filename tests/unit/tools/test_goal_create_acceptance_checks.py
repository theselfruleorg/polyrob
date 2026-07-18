"""§4.4 producer plumbing — the agent (goal_create), the operator (seed_goal
--check) and the planner prompt can attach typed acceptance_checks. NO create
gate: a goal without checks is always accepted."""
import asyncio


def test_goal_create_stores_typed_checks(tmp_path):
    from agents.task.goals.board import GoalBoard
    from tools.goal_tools import GoalTool, GoalCreateAction

    board = GoalBoard(str(tmp_path / "goals.db"))
    tool = GoalTool.__new__(GoalTool)
    tool._resolve_board = lambda: board
    tool._user = lambda ec: "u1"

    params = GoalCreateAction(
        title="write the report", body="do it",
        acceptance_checks=[{"type": "artifact_glob", "pattern": "*.md"}])
    res = asyncio.run(GoalTool.goal_create(tool, params))
    assert not res.error
    goals = board.list(user_id="u1")
    assert goals and (goals[0].payload or {}).get("acceptance_checks") == [
        {"type": "artifact_glob", "pattern": "*.md"}]


def test_goal_create_without_checks_is_accepted(tmp_path):
    from agents.task.goals.board import GoalBoard
    from tools.goal_tools import GoalTool, GoalCreateAction

    board = GoalBoard(str(tmp_path / "goals.db"))
    tool = GoalTool.__new__(GoalTool)
    tool._resolve_board = lambda: board
    tool._user = lambda ec: "u1"
    res = asyncio.run(GoalTool.goal_create(tool, GoalCreateAction(title="fuzzy goal")))
    assert not res.error, "NO create gate — arbitrary goals stay accepted (§4.4)"


def test_goal_create_drops_malformed_checks(tmp_path):
    from agents.task.goals.board import GoalBoard
    from tools.goal_tools import GoalTool, GoalCreateAction

    board = GoalBoard(str(tmp_path / "goals.db"))
    tool = GoalTool.__new__(GoalTool)
    tool._resolve_board = lambda: board
    tool._user = lambda ec: "u1"
    res = asyncio.run(GoalTool.goal_create(tool, GoalCreateAction(
        title="write it", acceptance_checks=[{"no_type": True}, "not-a-dict"])))
    assert not res.error
    goals = board.list(user_id="u1")
    assert not (goals[0].payload or {}).get("acceptance_checks"), \
        "malformed checks are dropped, not stored"


def test_seed_goal_check_arg_parses():
    from scripts.seed_goal import parse_check
    assert parse_check("artifact_glob:*.md") == {"type": "artifact_glob", "pattern": "*.md"}
    assert parse_check("http_ok:https://example.com/x") == {
        "type": "http_ok", "url": "https://example.com/x"}


def test_planner_prompt_encourages_checks(tmp_path):
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.planner import build_planner_prompt

    board = GoalBoard(str(tmp_path / "goals.db"))
    prompt = build_planner_prompt(board, "u1", str(tmp_path))
    assert "acceptance_checks" in prompt
