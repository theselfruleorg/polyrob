"""WS-B2 — hook fail-modes + telemetry.

Hooks may register with fail_mode="open" (default, legacy: swallow + continue)
or fail_mode="closed" (guardrail/billing: a crashing hook must NOT pass silently):
  - pre   closed + raise -> DENY the action
  - transform closed + raise -> replace result with an error ActionResult
  - post  closed + raise -> propagate
Every hook exception is logged at ERROR (telemetry signal), in both modes.
"""
import logging

import pytest

import agents.task.agent.service  # noqa: F401 — avoid import cycle
from tools.controller.service import Controller
from tools.controller.types import ActionResult


def _bare_controller() -> Controller:
    c = object.__new__(Controller)
    c.logger = logging.getLogger("hook-failmode-test")
    c._pre_tool_call_hooks = []
    c._post_tool_call_hooks = []
    c._transform_tool_result_hooks = []
    return c


def _boom(*a, **k):
    raise RuntimeError("hook bug")


# --- pre ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_open_hook_failure_allows(caplog):
    c = _bare_controller()
    c.register_pre_tool_call_hook(_boom)  # default open
    with caplog.at_level(logging.ERROR):
        reason = await c._run_pre_tool_call_hooks("write_file", {}, None)
    assert reason is None  # fail-open: allowed
    assert any("hook" in r.message.lower() for r in caplog.records)  # but logged


@pytest.mark.asyncio
async def test_pre_closed_hook_failure_denies():
    c = _bare_controller()
    c.register_pre_tool_call_hook(_boom, fail_mode="closed")
    reason = await c._run_pre_tool_call_hooks("write_file", {}, None)
    assert reason  # truthy denial reason
    assert "hook" in reason.lower()


# --- transform ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_transform_closed_hook_failure_returns_error_result():
    c = _bare_controller()
    c.register_transform_tool_result_hook(_boom, fail_mode="closed")
    r = ActionResult(extracted_content="safe")
    out = await c._run_transform_tool_result_hooks("read_file", {}, r, None)
    assert out is not r
    assert out.error  # replaced with an error result


# --- post --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_closed_hook_failure_propagates():
    c = _bare_controller()
    c.register_post_tool_call_hook(_boom, fail_mode="closed")
    with pytest.raises(RuntimeError):
        await c._run_post_tool_call_hooks("write_file", {}, ActionResult(extracted_content="x"), None)


# --- backward-compat: existing bare-callable registration still works --------

@pytest.mark.asyncio
async def test_open_is_default_and_backward_compatible():
    c = _bare_controller()
    seen = []
    c.register_post_tool_call_hook(lambda n, p, r, ctx: seen.append(n))
    await c._run_post_tool_call_hooks("t", {}, ActionResult(extracted_content="x"), None)
    assert seen == ["t"]
