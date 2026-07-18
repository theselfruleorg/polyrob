"""Interactive OWNER telegram session gets the introspection + mission toolset.

Bug (2026-07-03): an inbound telegram message created a session with the bare
SessionRequest default ['browser','filesystem','task'] — no `goal`, so when the owner
asked "review your goals" the agent had no goal_list tool, fell back to reading its
session sandbox, and hallucinated "goal database is empty / repo missing". The owner
principal's interactive session must get `goal` (+ twitter/web_fetch) so it can actually
introspect its board and act on-mission. A non-owner keeps the conservative default.
"""
import pytest

from surfaces.telegram.interactive_tools import owner_interactive_tool_ids


def test_owner_gets_goal_and_mission_tools(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    tools = owner_interactive_tool_ids("rob")
    assert tools is not None
    assert "goal" in tools          # can introspect the board (no more filesystem-guessing)
    assert "twitter" in tools       # can act on-mission (post)
    assert "web_fetch" in tools


def test_non_owner_keeps_default(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    assert owner_interactive_tool_ids("u_deadbeef") is None


def test_owner_tool_ids_env_overridable(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setenv("INTERACTIVE_TOOL_IDS", "goal,filesystem,task")
    tools = owner_interactive_tool_ids("rob")
    assert tools == ["goal", "filesystem", "task"]


# --- proposal 014-A2: mode- and posture-aware interactive toolset -------------
# Posture pattern per tests/unit/agents/task/goals/test_posture_toolset.py:16-30
# (AGENT_COMPUTE_POSTURE is frozen at import — refreeze around every change).


def _enable_full(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")


def test_supervised_interactive_default_unchanged(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("INTERACTIVE_TOOL_IDS", raising=False)
    from surfaces.telegram.interactive_tools import interactive_tool_ids
    assert interactive_tool_ids() == ["goal", "twitter", "web_fetch", "filesystem", "task"]


def test_autonomous_interactive_widens_and_env_still_wins(monkeypatch):
    _enable_full(monkeypatch)
    monkeypatch.delenv("INTERACTIVE_TOOL_IDS", raising=False)
    from surfaces.telegram.interactive_tools import interactive_tool_ids
    tools = interactive_tool_ids()
    for t in ("coding", "email", "anysite", "goal", "cronjob"):
        assert t in tools                    # owner chat KEEPS the meta tools
    for t in ("x402_pay", "hyperliquid", "polymarket"):
        assert t not in tools                # money-spend never via mode
    monkeypatch.setenv("INTERACTIVE_TOOL_IDS", "filesystem,task")
    assert interactive_tool_ids() == ["filesystem", "task"]


def test_autonomous_interactive_adds_compute_at_posture_1(monkeypatch):
    import agents.task.constants as c
    _enable_full(monkeypatch)
    monkeypatch.delenv("INTERACTIVE_TOOL_IDS", raising=False)
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    c._refreeze_compute_posture_for_tests()
    try:
        from surfaces.telegram.interactive_tools import interactive_tool_ids
        tools = interactive_tool_ids()
        for t in ("code_execution", "shell", "coding"):
            assert t in tools
    finally:
        monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
        c._refreeze_compute_posture_for_tests()


def test_supervised_never_gets_compute_even_at_posture_1(monkeypatch):
    import agents.task.constants as c
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("INTERACTIVE_TOOL_IDS", raising=False)
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    c._refreeze_compute_posture_for_tests()
    try:
        from surfaces.telegram.interactive_tools import interactive_tool_ids
        assert "shell" not in interactive_tool_ids()
    finally:
        monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
        c._refreeze_compute_posture_for_tests()
