"""Proposal 001: goal_create accepts a `tools` list, filtered to a safe allowlist.

Owner-approved option 2 (2026-07-01): Rob's self-created goals may request research/content/coding
tools AND twitter, but never money (wallet/x402/hyperliquid/polymarket), code execution, cron, or
meta goal/skill tools.
"""
import asyncio

from agents.task.goals.board import GoalBoard
from tools.goal_tools import GoalTool, GoalCreateAction, _SELF_GOAL_ALLOWED_TOOLS


class _Ctx:
    user_id = "tester"


def _make_tool(tmp_path):
    tool = GoalTool.__new__(GoalTool)  # skip BaseTool.__init__ (needs a full container)
    tool._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return tool


def test_allowlist_shape():
    for safe in ("filesystem", "task", "coding", "web_fetch", "twitter"):
        assert safe in _SELF_GOAL_ALLOWED_TOOLS
    for danger in ("x402_pay", "hyperliquid", "polymarket", "code_execution", "cronjob", "goal", "wallet"):
        assert danger not in _SELF_GOAL_ALLOWED_TOOLS


def test_goal_create_keeps_allowlisted_drops_blocked(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest goal", body="b",
                         tools=["coding", "twitter", "x402_pay", "cronjob", "wallet"]),
        _Ctx(),
    ))
    txt = res.extracted_content
    assert "coding" in txt and "twitter" in txt
    assert "x402_pay" not in txt and "cronjob" not in txt and "wallet" not in txt


def test_goal_create_without_tools_has_no_toolset(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(GoalCreateAction(title="captest goal 2", body="b"), _Ctx()))
    assert "tools=" not in res.extracted_content
