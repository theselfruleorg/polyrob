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


def test_owner_gets_the_board_and_a_web_read(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.delenv("INTERACTIVE_TOOL_IDS", raising=False)
    tools = owner_interactive_tool_ids("rob")
    assert tools is not None
    assert "goal" in tools          # can introspect the board (no more filesystem-guessing)
    assert "cronjob" in tools       # the owner chat is where scheduling is asked about
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


def test_supervised_interactive_default_is_the_owner_rig(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("INTERACTIVE_TOOL_IDS", raising=False)
    from core.config_policy.rigs import rig_tools
    from surfaces.telegram.interactive_tools import interactive_tool_ids
    assert interactive_tool_ids() == rig_tools("owner")


# --- 081 WS-3.1: the owner chat runs on the owner rig + the granted money -----


def test_autonomous_owner_chat_is_the_rig_not_the_whole_grant(monkeypatch):
    """Owner chat stopped shipping the whole grant (~194 schemas, 53K of a 90K
    first call). The rest is one load_tool away; env still wins."""
    _enable_full(monkeypatch)
    monkeypatch.delenv("INTERACTIVE_TOOL_IDS", raising=False)
    monkeypatch.delenv("DEFI_AGENT_AUTONOMY", raising=False)
    from surfaces.telegram.interactive_tools import interactive_tool_ids
    tools = interactive_tool_ids()
    for t in ("goal", "cronjob", "filesystem", "task", "web_fetch"):
        assert t in tools
    for t in ("coding", "email", "anysite", "twitter", "browser", "x_browser"):
        assert t not in tools                # loadable from the catalog
    for t in ("x402_pay", "hyperliquid", "polymarket", "defi_trade"):
        assert t not in tools                # money-spend never via mode
    monkeypatch.setenv("INTERACTIVE_TOOL_IDS", "filesystem,task")
    assert interactive_tool_ids() == ["filesystem", "task"]


def test_a_granted_money_tool_stays_eager(monkeypatch):
    """load_tool never loads money, so the armed grant's money tools are eager."""
    _enable_full(monkeypatch)
    monkeypatch.delenv("INTERACTIVE_TOOL_IDS", raising=False)
    monkeypatch.setenv("DEFI_AGENT_AUTONOMY", "true")
    from core.tool_capabilities import TOOL_CAPABILITIES
    from surfaces.telegram.interactive_tools import interactive_tool_ids
    tools = interactive_tool_ids()
    assert "defi_trade" in tools
    assert "defi_data" not in tools          # read-only: one load_tool away
    money = [t for t in tools if "money" in TOOL_CAPABILITIES.get(t, ())]
    assert "defi_trade" in money


def test_compute_tools_are_loadable_not_eager_at_posture_1(monkeypatch):
    import agents.task.constants as c
    _enable_full(monkeypatch)
    monkeypatch.delenv("INTERACTIVE_TOOL_IDS", raising=False)
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    monkeypatch.setenv("CODE_EXEC_ENABLED", "true")
    monkeypatch.setenv("SHELL_TOOLS_ENABLED", "true")
    c._refreeze_compute_posture_for_tests()
    try:
        from surfaces.telegram.interactive_tools import interactive_tool_ids
        tools = interactive_tool_ids()
        for t in ("code_execution", "shell"):
            assert t not in tools
    finally:
        monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
        c._refreeze_compute_posture_for_tests()
