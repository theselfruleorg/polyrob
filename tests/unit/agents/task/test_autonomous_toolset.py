"""AUTONOMOUS_MODE_TOOLS + planner web_fetch + goal-tool vocabulary reconciliation
(proposal 013 T3). Patch env via monkeypatch — never reload (see
tests/unit/agents/task/test_autonomy_mode.py, the committed T1 pattern)."""
import pytest

from agents.task import constants


def _enable_full(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    constants.reset_autonomy_mode_warnings()


MONEY_AND_HOST = {"x402_pay", "wallet", "hyperliquid", "polymarket",
                  "code_execution", "shell", "self_env", "process"}


def test_constant_excludes_money_and_host():
    assert MONEY_AND_HOST.isdisjoint(set(constants.AUTONOMOUS_MODE_TOOLS))


def test_default_goal_tools_supervised_unchanged(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "0")
    constants._refreeze_compute_posture_for_tests()
    from agents.task.goals.dispatcher import default_goal_tools
    assert default_goal_tools() == ["filesystem", "task"]


def test_default_goal_tools_autonomous_full_set(monkeypatch):
    _enable_full(monkeypatch)
    from agents.task.goals.dispatcher import default_goal_tools
    tools = default_goal_tools()
    for t in ("web_fetch", "twitter", "email", "knowledge", "anysite", "x402_invoice"):
        assert t in tools
    assert MONEY_AND_HOST.isdisjoint(set(tools))


def test_cron_mirrors_goal_default(monkeypatch):
    _enable_full(monkeypatch)
    from cron.runner import default_cron_tools
    from agents.task.goals.dispatcher import default_goal_tools
    assert default_cron_tools() == default_goal_tools()


def test_planner_gets_web_fetch_under_autonomous(monkeypatch):
    _enable_full(monkeypatch)
    from agents.task.goals import planner
    assert "web_fetch" in planner.planner_session_tools()
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    assert planner.planner_session_tools() == list(planner.PLANNER_TOOLS)


def test_valid_tool_ids_covers_autonomous_set():
    from agents.task.agent.skill_manager import VALID_TOOL_IDS
    missing = set(constants.AUTONOMOUS_MODE_TOOLS) - VALID_TOOL_IDS
    assert not missing, f"VALID_TOOL_IDS is missing autonomous-set ids: {missing}"


def test_server_default_tools_supervised_unchanged(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    from agents.task.tool_defaults import server_default_tools
    assert server_default_tools() == ['filesystem', 'task', 'web_fetch', 'perplexity',
                                       'email', 'mcp', 'anysite']


def test_server_default_tools_autonomous_excludes_goal_and_cronjob(monkeypatch):
    _enable_full(monkeypatch)
    from agents.task.tool_defaults import server_default_tools
    tools = server_default_tools()
    assert "goal" not in tools and "cronjob" not in tools
    assert MONEY_AND_HOST.isdisjoint(set(tools))


def test_allowed_self_goal_tools_supervised_matches_frozen_set(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    from tools.goal_tools import allowed_self_goal_tools, _SELF_GOAL_ALLOWED_TOOLS
    assert allowed_self_goal_tools() == _SELF_GOAL_ALLOWED_TOOLS
    assert "goal" not in allowed_self_goal_tools()
    assert "cronjob" not in allowed_self_goal_tools()


def test_allowed_self_goal_tools_autonomous_expands(monkeypatch):
    _enable_full(monkeypatch)
    from tools.goal_tools import allowed_self_goal_tools
    tools = allowed_self_goal_tools()
    assert "goal" in tools and "cronjob" in tools
    assert MONEY_AND_HOST.isdisjoint(tools)


def test_allowed_self_goal_tools_never_includes_money_or_host(monkeypatch):
    for mode_setup in (lambda mp: mp.delenv("AUTONOMY_MODE", raising=False), _enable_full):
        mode_setup(monkeypatch)
        from tools.goal_tools import allowed_self_goal_tools
        assert MONEY_AND_HOST.isdisjoint(allowed_self_goal_tools())


def test_goal_create_filters_goal_tool_supervised(monkeypatch):
    """Regression: a goal_create requesting 'goal'/'cronjob' is filtered out under
    supervised mode exactly as today (uses the module-level constant, unaffected)."""
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    from tools.goal_tools import _SELF_GOAL_ALLOWED_TOOLS
    requested = ["filesystem", "goal", "cronjob", "twitter"]
    allowed = [t for t in requested if t in _SELF_GOAL_ALLOWED_TOOLS]
    assert "goal" not in allowed and "cronjob" not in allowed
