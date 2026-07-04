"""P2 — post_tool_call (observe) + transform_tool_result (rewrite) hook seams.

Completes the Reference plugin trio alongside pre_tool_call. Bare-controller style,
matching test_pre_tool_call_hook.py.
"""
import logging

import pytest

import agents.task.agent.service  # noqa: F401 — avoid import cycle
from tools.controller.service import Controller
from tools.controller.types import ActionResult


def _bare_controller() -> Controller:
    c = object.__new__(Controller)
    c.logger = logging.getLogger("post-transform-test")
    c._post_tool_call_hooks = []
    c._transform_tool_result_hooks = []
    return c


# --- post_tool_call (observe-only) -------------------------------------------

@pytest.mark.asyncio
async def test_no_post_hooks_is_noop():
    c = _bare_controller()
    # must not raise
    await c._run_post_tool_call_hooks("write_file", {}, ActionResult(extracted_content="x"), None)


@pytest.mark.asyncio
async def test_post_hook_observes_result():
    c = _bare_controller()
    seen = []
    c.register_post_tool_call_hook(lambda name, params, result, ctx: seen.append((name, result.extracted_content)))
    await c._run_post_tool_call_hooks("write_file", {"p": 1}, ActionResult(extracted_content="hello"), None)
    assert seen == [("write_file", "hello")]


@pytest.mark.asyncio
async def test_post_hook_failure_is_fail_open():
    c = _bare_controller()

    def boom(name, params, result, ctx):
        raise RuntimeError("hook bug")

    c.register_post_tool_call_hook(boom)
    # must not raise
    await c._run_post_tool_call_hooks("write_file", {}, ActionResult(extracted_content="x"), None)


# --- transform_tool_result (rewrite) -----------------------------------------

@pytest.mark.asyncio
async def test_no_transform_hooks_returns_original():
    c = _bare_controller()
    r = ActionResult(extracted_content="original")
    out = await c._run_transform_tool_result_hooks("read_file", {}, r, None)
    assert out is r


@pytest.mark.asyncio
async def test_transform_hook_can_replace_result():
    c = _bare_controller()
    c.register_transform_tool_result_hook(
        lambda name, params, result, ctx: ActionResult(extracted_content="[redacted]")
        if "secret" in (result.extracted_content or "") else None
    )
    out = await c._run_transform_tool_result_hooks("read_file", {}, ActionResult(extracted_content="my secret"), None)
    assert out.extracted_content == "[redacted]"


@pytest.mark.asyncio
async def test_transform_hook_none_keeps_previous():
    c = _bare_controller()
    c.register_transform_tool_result_hook(lambda name, params, result, ctx: None)
    r = ActionResult(extracted_content="keep me")
    out = await c._run_transform_tool_result_hooks("read_file", {}, r, None)
    assert out is r


@pytest.mark.asyncio
async def test_transform_hooks_chain_in_order():
    c = _bare_controller()
    c.register_transform_tool_result_hook(
        lambda name, params, result, ctx: ActionResult(extracted_content=(result.extracted_content or "") + "-a")
    )
    c.register_transform_tool_result_hook(
        lambda name, params, result, ctx: ActionResult(extracted_content=(result.extracted_content or "") + "-b")
    )
    out = await c._run_transform_tool_result_hooks("x", {}, ActionResult(extracted_content="r"), None)
    assert out.extracted_content == "r-a-b"


@pytest.mark.asyncio
async def test_transform_hook_failure_is_fail_open():
    c = _bare_controller()

    def boom(name, params, result, ctx):
        raise RuntimeError("hook bug")

    c.register_transform_tool_result_hook(boom)
    r = ActionResult(extracted_content="safe")
    out = await c._run_transform_tool_result_hooks("x", {}, r, None)
    assert out is r  # original preserved on hook failure
