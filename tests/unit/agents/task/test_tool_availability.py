"""Tool-availability transparency (proposal 013 T8, owner directive 2026-07-15).

A missing capability must always be disclosed (gate + remedy), never guessed at or
used as an excuse. Covers the registry, the prompt note, the planner grounding block,
and the SystemPrompt injection point.
"""
import pytest

from agents.task import constants
from agents.task.agent.core import tool_availability as ta


def test_registry_covers_the_stall_tools():
    for tool in ("twitter", "x_browser", "mcp", "email", "x402_invoice",
                 "code_execution", "shell"):
        assert tool in ta.GATED_TOOL_REGISTRY


def test_x_browser_remedy_names_enablement_and_session_capture():
    gate, tier, remedy = ta.GATED_TOOL_REGISTRY["x_browser"]
    assert tier == "disabled"
    assert "X_BROWSER_ENABLED" in gate
    assert "capture-session" in remedy


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


# --- compute gates name the limb that is ACTUALLY unmet (Rob's self-review 2026-09-19 #2:
# prod ran posture 1 with CODE_EXEC_ENABLED=false and the note still told the agent to
# "raise AGENT_COMPUTE_POSTURE" — a remedy the deploy already satisfied).

def _note_line(note: str, tool: str) -> str:
    for line in note.splitlines():
        if line.startswith(f"- {tool} ["):
            return line
    raise AssertionError(f"no line for {tool} in note:\n{note}")


@pytest.fixture
def _compute_env(monkeypatch):
    import importlib
    cp = importlib.import_module("core.config_policy.compute_posture")
    monkeypatch.delenv("TOOL_AVAILABILITY_HINT", raising=False)
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)

    def _set(posture, code_exec, shell):
        monkeypatch.setenv("AGENT_COMPUTE_POSTURE", str(posture))
        monkeypatch.setenv("CODE_EXEC_ENABLED", code_exec)
        monkeypatch.setenv("SHELL_TOOLS_ENABLED", shell)
        cp._refreeze_compute_posture()

    yield _set
    cp._refreeze_compute_posture()


def test_code_exec_gate_names_the_flag_when_posture_is_already_met(_compute_env):
    _compute_env(1, "false", "false")
    line = _note_line(ta.build_tool_availability_note(set()), "code_execution")
    assert "CODE_EXEC_ENABLED=false" in line
    assert "posture 1 already met" in line
    # the stale remedy must not survive: raising the posture changes nothing here
    assert "raises AGENT_COMPUTE_POSTURE" not in line
    assert "CODE_EXEC_ENABLED=true" in line


def test_code_exec_gate_names_posture_when_that_is_the_unmet_limb(_compute_env):
    _compute_env(0, "true", "false")
    line = _note_line(ta.build_tool_availability_note(set()), "code_execution")
    assert "AGENT_COMPUTE_POSTURE=0" in line
    assert "AGENT_COMPUTE_POSTURE>=1" in line
    assert "CODE_EXEC_ENABLED=false" not in line


def test_code_exec_gate_when_both_limbs_met_names_the_session_toolset(_compute_env):
    _compute_env(1, "true", "true")
    note = ta.build_tool_availability_note(set())
    line = _note_line(note, "code_execution")
    assert "[loadable]" in line
    assert "not in this session" in line
    assert "interactive" in line
    sline = _note_line(note, "shell")
    assert "[loadable]" in sline


def test_shell_gate_names_its_own_flag_at_posture_one(_compute_env):
    _compute_env(1, "false", "false")
    line = _note_line(ta.build_tool_availability_note(set()), "shell")
    assert "SHELL_TOOLS_ENABLED=false" in line
    assert "posture 1 already met" in line


def test_compute_gate_helper_fails_open_to_registry(monkeypatch):
    monkeypatch.setattr(ta, "_compute_limbs", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    gate, tier, remedy = ta.compute_gate("code_execution")
    assert (gate, tier, remedy) == ta.GATED_TOOL_REGISTRY["code_execution"]
