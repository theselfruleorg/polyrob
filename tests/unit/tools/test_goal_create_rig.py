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
        GoalCreateAction(title="post the weekly thread", tools=["twitter"], rig="research"),
        _Ctx()))
    goal = tool._resolve_board().list_recent(user_id="tester", limit=5)[0]
    assert goal.payload["rig"] == "research"
    assert "twitter" in goal.payload["tools"]
    assert resolve_rig_tools(goal.payload, None) == goal.payload["tools"]


# --- H05 (security analysis 2026-09-23): an agent-set rig cannot widen -------

import pytest


@pytest.fixture
def _supervised(monkeypatch):
    """The historical allowlist: no full autonomy, no defi autonomy."""
    monkeypatch.setattr("tools.goal_tools.allowed_self_goal_tools",
                        lambda: __import__("tools.goal_tools", fromlist=["x"])
                        ._SELF_GOAL_ALLOWED_TOOLS)


@pytest.mark.parametrize("rig,leaks", [
    ("money_rail", "defi_trade"), ("social", "x_browser"), ("ops", "cronjob")])
def test_agent_rig_past_the_ceiling_is_refused(tmp_path, _supervised, rig, leaks):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="read the docs and summarise", rig=rig), _Ctx()))
    assert res.error and "NOT granted" in res.error and leaks in res.error
    assert "research" in res.error  # names the rigs it CAN use
    assert tool._resolve_board().list_recent(user_id="tester", limit=5) == []


def test_full_rig_and_fitting_rig_are_accepted_and_stamped(tmp_path, _supervised):
    tool = _make_tool(tmp_path)
    titles = {"full": "draft the quarterly newsletter", "research": "survey vector databases"}
    for rig, title in titles.items():
        res = asyncio.run(tool.goal_create(GoalCreateAction(title=title, rig=rig), _Ctx()))
        assert res.error is None, res.error
    for goal in tool._resolve_board().list_recent(user_id="tester", limit=5):
        assert goal.payload["authored_by"] == "agent"


def test_dispatch_intersects_a_legacy_agent_rig(_supervised):
    """A row written BEFORE the refusal existed is narrowed at dispatch."""
    from core.config_policy.rigs import resolve_rig_tools
    from tools.goal_tools import allowed_self_goal_tools
    ceiling = sorted(allowed_self_goal_tools())
    legacy = {"rig": "money_rail", "created_by_session_id": "s1"}
    got = resolve_rig_tools(legacy, ["x"], agent_ceiling=ceiling)
    assert "defi_trade" not in got and "defi_data" in got
    marked = {"rig": "ops", "authored_by": "agent"}
    assert "cronjob" not in resolve_rig_tools(marked, ["x"], agent_ceiling=ceiling)


def test_dispatch_honours_an_owner_set_rig(_supervised):
    from core.config_policy.rigs import RIGS, resolve_rig_tools
    from tools.goal_tools import allowed_self_goal_tools
    got = resolve_rig_tools({"rig": "money_rail"}, ["x"],
                            agent_ceiling=sorted(allowed_self_goal_tools()))
    assert got == list(RIGS["money_rail"])
