"""057 WS-A: goal_create carries a named tool rig."""
import asyncio

from agents.task.goals.board import GoalBoard
from tools.goal_tools import GoalTool, GoalCreateAction


class _Ctx:
    user_id = "tester"


def _make_tool(tmp_path):
    tool = GoalTool.__new__(GoalTool)  # skip BaseTool.__init__ (needs a container)
    tool._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return tool


def test_rig_is_a_typed_optional_field():
    assert GoalCreateAction(title="ship it").rig is None
    assert GoalCreateAction(title="ship it", rig="research").rig == "research"


def test_valid_rig_lands_on_the_payload(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="read the docs and summarise", rig="Research"), _Ctx()))
    assert res.error is None
    goal = tool._resolve_board().list_recent(user_id="tester", limit=5)[0]
    assert goal.payload["rig"] == "research"  # normalised


def test_unknown_rig_is_refused_with_the_valid_list(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="read the docs and summarise", rig="reserch"), _Ctx()))
    assert res.error and "reserch" in res.error and "research" in res.error
    assert tool._resolve_board().list_recent(user_id="tester", limit=5) == []


def test_explicit_tools_and_a_rig_both_survive_to_dispatch(tmp_path):
    """`tools` wins at dispatch; the rig is still recorded so the author's
    stated intent survives an edit."""
    from core.config_policy.rigs import resolve_rig_tools
    tool = _make_tool(tmp_path)
    asyncio.run(tool.goal_create(
        GoalCreateAction(title="post the weekly thread", tools=["twitter"], rig="social"),
        _Ctx()))
    goal = tool._resolve_board().list_recent(user_id="tester", limit=5)[0]
    assert goal.payload["rig"] == "social"
    assert "twitter" in goal.payload["tools"]
    assert resolve_rig_tools(goal.payload, None) == goal.payload["tools"]
