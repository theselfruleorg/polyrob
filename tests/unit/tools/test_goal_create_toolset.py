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


# --- Proposal 009 (2026-07-14, battle-test night-1): mission tools + text inference ---

def test_allowlist_includes_kickoff_mission_tools():
    """Owner kickoff (2026-07-13) sanctions email outreach, telegram posting and
    x402 invoicing for self-created goals; spend-side stays excluded."""
    for mission in ("email", "message", "x402_invoice", "knowledge"):
        assert mission in _SELF_GOAL_ALLOWED_TOOLS
    for danger in ("x402_pay", "hyperliquid", "polymarket", "code_execution", "cronjob", "goal", "wallet"):
        assert danger not in _SELF_GOAL_ALLOWED_TOOLS


def test_goal_create_infers_tools_from_text_when_unset(tmp_path):
    """A goal whose text names a capability gets that tool + the safe baseline —
    the exact night-1 failure ('Publish queued OSS launch X thread' dispatched
    without twitter)."""
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="Publish queued OSS launch X thread",
                         body="post the tweet thread", acceptance="live tweet url"),
        _Ctx(),
    ))
    txt = res.extracted_content
    assert "twitter" in txt
    # baseline rides along so the session isn't starved of basics
    assert "filesystem" in txt and "task" in txt and "web_fetch" in txt


def test_goal_create_inference_covers_mission_surfaces(tmp_path):
    tool = _make_tool(tmp_path)
    cases = {
        "Use rob mailbox for registrations": ("send email signups", "email"),
        "Find x402 services and earn": ("issue an invoice for value", "x402_invoice"),
        "Introduce yourself in t.me/thepublicden": ("post in the telegram group", "message"),
        "Re-learn POLYROB docs": ("fetch polyrob.dev and update notes", "web_fetch"),
    }
    for title, (body, expected) in cases.items():
        res = asyncio.run(tool.goal_create(GoalCreateAction(title=title, body=body), _Ctx()))
        assert expected in res.extracted_content, (title, res.extracted_content)


def test_goal_create_explicit_tools_get_baseline_union(tmp_path):
    """Explicit tools=['twitter'] must not strand the session without basics."""
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest explicit union", body="b", tools=["twitter"]),
        _Ctx(),
    ))
    txt = res.extracted_content
    assert "twitter" in txt and "filesystem" in txt and "task" in txt


def test_goal_create_inference_never_grants_money_spend(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest pay wallet hyperliquid x402_pay goal",
                         body="x402_pay wallet hyperliquid polymarket cron"),
        _Ctx(),
    ))
    txt = res.extracted_content
    # assert on the granted toolset segment, not the echoed title
    assert "tools=" in txt
    toolset = txt.split("tools=", 1)[1].split(":", 1)[0]
    for danger in ("x402_pay", "wallet", "hyperliquid", "polymarket", "cronjob"):
        assert danger not in toolset
    # ("x402" token in the text legitimately infers the capped x402_invoice receivable tool)
    assert "x402_invoice" in toolset
