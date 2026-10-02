"""goal_create stamps payload.origin_session_id ONLY for a genuine owner turn.

The dispatcher self-wakes the origin session when the goal completes
(2026-08-28: never the run session itself). A planner/goal/cron session or a
forged re-entry must not become an origin — it is finished by then, and a
planner session woken with a completion could queue more goals.
"""
import asyncio

from agents.task.goals.board import GoalBoard
from tools.goal_tools import GoalTool, GoalCreateAction


class _Ctx:
    def __init__(self, session_id, role="orchestrator", is_sub_agent=False, metadata=None):
        self.user_id = "tester"
        self.session_id = session_id
        self.role = role
        self.is_sub_agent = is_sub_agent
        self.metadata = metadata or {}


def _tool(tmp_path):
    tool = GoalTool.__new__(GoalTool)
    tool._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return tool


def _create(tool, ctx, title):
    asyncio.run(tool.goal_create(GoalCreateAction(title=title, body="b"), ctx))
    return tool._goal_board.list(user_id="tester", limit=5)[0]


def test_genuine_owner_turn_is_stamped(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.task.goals.autonomy_marker.is_autonomous",
                        lambda sid: False, raising=False)
    g = _create(_tool(tmp_path), _Ctx("chat-1"), "owner asked for this")
    assert g.payload.get("origin_session_id") == "chat-1"


def test_autonomous_planner_session_is_not_an_origin(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.task.goals.autonomy_marker.is_autonomous",
                        lambda sid: sid == "planner-1", raising=False)
    g = _create(_tool(tmp_path), _Ctx("planner-1"), "planner queued this")
    assert "origin_session_id" not in g.payload


def test_forged_reentry_and_leaf_turns_are_not_origins(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.task.goals.autonomy_marker.is_autonomous",
                        lambda sid: False, raising=False)
    tool = _tool(tmp_path)
    g = _create(tool, _Ctx("chat-2", metadata={"turn_kind": "self_wake"}), "from a self-wake")
    assert "origin_session_id" not in g.payload
    r = asyncio.run(tool.goal_create(GoalCreateAction(title="from a leaf worker", body="b"),
                                     _Ctx("chat-3", role="leaf")))
    assert r.error and "leaf" in r.error


def test_a_leaf_or_sub_agent_cannot_create_a_durable_goal(tmp_path):
    """Review E2: cronjob is delegate_blocked and goal_ask / rail_propose refuse a
    leaf; goal_create let a delegated leaf queue durable autonomous work."""
    tool = _tool(tmp_path)
    for ctx in (_Ctx("leaf-1", role="leaf"), _Ctx("sub-1", is_sub_agent=True)):
        r = asyncio.run(tool.goal_create(GoalCreateAction(title="durable work", body="b"), ctx))
        assert r.error and "leaf/sub-agent" in r.error
    assert tool._goal_board.list(user_id="tester", limit=5) == []
