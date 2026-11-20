"""U5 + U7 + A-9: the self-goal tool ceiling, its baseline and the text inference
live in ``core.tool_grants``; the delegation policy lives in
``core.delegation_policy``. A ceiling probe that raises is logged, not swallowed."""
import logging

import pytest


def test_self_goal_grants_are_core():
    from core import tool_grants as tg
    assert isinstance(tg.SELF_GOAL_BASELINE_TOOLS, tuple) and tg.SELF_GOAL_BASELINE_TOOLS
    assert tg.allowed_self_goal_tools() >= tg.SELF_GOAL_ALLOWED_TOOLS
    assert "twitter" in tg.infer_tools_from_text("Post the launch tweet") or \
        "twitter" not in tg.allowed_self_goal_tools()
    assert tg.infer_tools_from_text(None, "") == set()


def test_inference_never_leaves_the_ceiling(monkeypatch):
    from core import tool_grants as tg
    monkeypatch.setattr(tg, "allowed_self_goal_tools", lambda: frozenset({"email"}))
    assert tg.infer_tools_from_text("email the invoice and tweet it") == {"email"}


def test_a_raising_autonomy_probe_is_logged_and_keeps_the_ceiling(monkeypatch, caplog):
    import core.config_policy as cp
    from core import tool_grants as tg

    def _boom():
        raise RuntimeError("probe broke")

    monkeypatch.setattr(cp, "full_autonomy_enabled", _boom)
    with caplog.at_level(logging.WARNING, logger="core.tool_grants"):
        got = tg.allowed_self_goal_tools()
    assert got == tg.SELF_GOAL_ALLOWED_TOOLS
    assert any("probe broke" in r.getMessage() for r in caplog.records)


def test_delegation_policy_has_one_home():
    import importlib.util
    import core.delegation_policy as dp
    assert callable(dp.evaluate_delegation)
    assert importlib.util.find_spec("tools.controller.delegation") is None   # the shim is gone


@pytest.mark.parametrize("rel", ["agents/task/goals/dispatcher.py",
                                 "agents/task/agent/core/tool_availability.py",
                                 "agents/task/agent/sub_agent_manager.py"])
def test_the_agents_tier_imports_the_grants_down(rel):
    import ast
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[3]
    mods = {n.module for n in ast.walk(ast.parse((repo / rel).read_text()))
            if isinstance(n, ast.ImportFrom)}
    assert not mods & {"tools.goal_tools", "tools.controller.delegation"}, rel
