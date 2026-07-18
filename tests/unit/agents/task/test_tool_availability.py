"""Tool-availability transparency (proposal 013 T8, owner directive 2026-07-15).

A missing capability must always be disclosed (gate + remedy), never guessed at or
used as an excuse. Covers the registry, the prompt note, the planner grounding block,
and the SystemPrompt injection point.
"""
import pytest

from agents.task import constants
from agents.task.agent.core import tool_availability as ta


def test_registry_covers_the_stall_tools():
    for tool in ("twitter", "mcp", "email", "x402_invoice", "code_execution", "shell"):
        assert tool in ta.GATED_TOOL_REGISTRY


def test_money_tools_marked_reserved():
    for tool in ("x402_pay", "hyperliquid", "polymarket"):
        gate, tier, remedy = ta.GATED_TOOL_REGISTRY[tool]
        assert tier == "reserved"
        assert "owner" in remedy.lower()


def test_note_lists_gated_and_omits_loaded(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("TOOL_AVAILABILITY_HINT", raising=False)
    note = ta.build_tool_availability_note(loaded_tool_ids={"twitter", "mcp"})
    # loaded tools are not re-listed as a missing entry
    assert "- twitter [" not in note
    assert "- mcp [" not in note
    # a not-loaded tool is still disclosed
    assert "- email [" in note
    assert "never a blocker" in note.lower()
    assert "</tool-availability>" in note


def test_note_empty_when_flag_off(monkeypatch):
    monkeypatch.setenv("TOOL_AVAILABILITY_HINT", "false")
    assert ta.build_tool_availability_note(loaded_tool_ids=set()) == ""


def test_grantable_matches_mode(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    supervised = set(ta.grantable_autonomous_tools())
    assert "twitter" in supervised  # _SELF_GOAL_ALLOWED_TOOLS already allows requesting it
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    constants.reset_autonomy_mode_warnings()
    assert set(constants.AUTONOMOUS_MODE_TOOLS) <= set(ta.grantable_autonomous_tools())


def test_planner_prompt_contains_tool_ceiling_guard(tmp_path):
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.planner import build_planner_prompt

    board = GoalBoard(str(tmp_path / "g.db"))
    board.create_objective(user_id="rob", title="Grow the substack")
    prompt = build_planner_prompt(board, "rob", None)
    assert "NEVER the ceiling" in prompt
    assert ("goals carry their OWN tools" in prompt
            or "carry their own tools" in prompt.lower())


# --- SystemPrompt injection -------------------------------------------------

def _prompt(tool_ids=("filesystem", "task")):
    from agents.task.agent.prompts import SystemPrompt
    return SystemPrompt(action_description="x", use_native_tools=True,
                        model_name="gpt-4", provider="openai", tool_ids=tool_ids)


def test_prompt_contains_availability_note_when_known_and_on(monkeypatch):
    monkeypatch.delenv("TOOL_AVAILABILITY_HINT", raising=False)
    content = _prompt().get_system_message().content
    assert "<tool-availability>" in content


def test_prompt_omits_availability_note_when_flag_off(monkeypatch):
    monkeypatch.setenv("TOOL_AVAILABILITY_HINT", "false")
    content = _prompt().get_system_message().content
    assert "<tool-availability>" not in content


def test_prompt_omits_availability_note_when_tool_ids_unknown(monkeypatch):
    monkeypatch.delenv("TOOL_AVAILABILITY_HINT", raising=False)
    content = _prompt(tool_ids=None).get_system_message().content
    assert "<tool-availability>" not in content
