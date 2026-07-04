"""Regression: param_model actions whose module uses `from __future__ import
annotations` must still dispatch the validated model as the first positional
arg — not splat its fields as kwargs.

Root cause (live bug, GLM CLI 2026-06-19): `tools/goal_tools.py` and
`tools/cronjob_tools.py` declare actions as `(self, params: SomeModel,
execution_context=None)` AND carry `from __future__ import annotations`, which
stringizes the `params` annotation to the str "SomeModel". The registry decided
how to call the method by `isinstance(first_anno, type) and issubclass(...)`,
which is False for a string, so it fell into the legacy splat path
(`func(**model.model_dump())`) → `TypeError: unexpected keyword argument 'title'`.

The registry must route by the explicitly-registered `param_model` when the
first parameter's annotation is the stringized name of that model.
"""
import logging

import pytest

from tools.controller.execution_context import ActionExecutionContext
from tools.controller.registry.service import Registry
from tools.goal_tools import GoalCreateAction, GoalListAction, GoalTool


def _goal_tool(tmp_path):
    class _Cfg:
        data_dir = str(tmp_path)

    t = object.__new__(GoalTool)
    t.logger = logging.getLogger("goal-stringized-test")
    t.config = _Cfg()
    t._goal_board = None
    return t


def _register(reg, tool, method_name, param_model):
    bound = getattr(GoalTool, method_name).__get__(tool, GoalTool)
    reg.wrap_function(method_name, bound, f"{method_name} desc", tool="goal", param_model=param_model)


@pytest.mark.asyncio
async def test_create_dispatches_model_not_kwargs(tmp_path):
    reg = Registry()
    tool = _goal_tool(tmp_path)
    _register(reg, tool, "goal_create", GoalCreateAction)
    ctx = ActionExecutionContext(session_id="s", user_id="u")

    result = await reg.execute_action(
        "goal_create", {"title": "Test goal alpha", "body": "verify"}, execution_context=ctx
    )

    assert getattr(result, "error", None) is None
    assert "Created goal" in (result.extracted_content or "")


@pytest.mark.asyncio
async def test_list_dispatches_model_not_kwargs(tmp_path):
    reg = Registry()
    tool = _goal_tool(tmp_path)
    _register(reg, tool, "goal_create", GoalCreateAction)
    _register(reg, tool, "goal_list", GoalListAction)
    ctx = ActionExecutionContext(session_id="s", user_id="u")

    await reg.execute_action("goal_create", {"title": "alpha goal", "body": ""}, execution_context=ctx)
    result = await reg.execute_action("goal_list", {}, execution_context=ctx)

    assert getattr(result, "error", None) is None
    assert "alpha goal" in (result.extracted_content or "")
