"""pre_tool_call hook seam on the Controller (flow-efficiency D2-a / Reference §23).

A single extension point that runs before each action executes and can veto it
(billing checks, approval, allow/deny lists). No hooks registered => no-op.
"""
import logging

import pytest

# Import via the agents side first to avoid a controller<->orchestrator import cycle
# when tools.controller.service is the first module loaded.
import agents.task.agent.service  # noqa: F401
from tools.controller.service import Controller, make_denylist_hook


def _bare_controller() -> Controller:
    c = object.__new__(Controller)
    c.logger = logging.getLogger("pre-hook-test")
    c._pre_tool_call_hooks = []
    return c


@pytest.mark.asyncio
async def test_no_hooks_allows_everything():
    c = _bare_controller()
    assert await c._run_pre_tool_call_hooks("write_file", {"file_path": "x"}, None) is None


@pytest.mark.asyncio
async def test_registered_hook_can_deny():
    c = _bare_controller()
    c.register_pre_tool_call_hook(
        lambda name, params, ctx: "not allowed" if name == "delete_file" else None
    )
    assert await c._run_pre_tool_call_hooks("write_file", {}, None) is None
    assert await c._run_pre_tool_call_hooks("delete_file", {}, None) == "not allowed"


@pytest.mark.asyncio
async def test_hook_exception_is_ignored_fails_open():
    c = _bare_controller()

    def boom(name, params, ctx):
        raise RuntimeError("hook bug")

    c.register_pre_tool_call_hook(boom)
    # a raising hook must not block execution
    assert await c._run_pre_tool_call_hooks("write_file", {}, None) is None


@pytest.mark.asyncio
async def test_denylist_hook_factory():
    c = _bare_controller()
    c.register_pre_tool_call_hook(make_denylist_hook(["delete_file", "create_directory"]))
    assert await c._run_pre_tool_call_hooks("delete_file", {}, None)  # truthy reason
    assert await c._run_pre_tool_call_hooks("write_file", {}, None) is None
