"""A goal whose acceptance is the goal board itself is refused at create.

2026-10-17 02:02–02:13Z: Rob made 5 "record row" goals ("A durable goal row
exists that names … Verified by goal_show read-back"). The completion judge
failed every one, which bred Unblock asks and more goals (~100 calls of churn).
"""
import asyncio

from agents.task.goals.board import GoalBoard
from tools.goal_tools import GoalCreateAction, GoalTool


def _tool(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    tool = GoalTool.__new__(GoalTool)
    tool._resolve_board = lambda: board
    tool._user = lambda ec: "u1"
    return tool, board


def test_goal_row_acceptance_is_refused(tmp_path):
    tool, board = _tool(tmp_path)
    res = asyncio.run(GoalTool.goal_create(tool, GoalCreateAction(
        title="bounty stream record",
        acceptance="A durable goal row exists that names the three runners. "
                   "Verified by goal_show read-back.")))
    assert res.error and "goal board itself" in res.error
    assert board.list(user_id="u1") == []


def test_ordinary_acceptance_is_still_accepted(tmp_path):
    tool, board = _tool(tmp_path)
    res = asyncio.run(GoalTool.goal_create(tool, GoalCreateAction(
        title="write the report", acceptance="docs/report.md exists and names the three runners")))
    assert not res.error
    assert len(board.list(user_id="u1")) == 1
