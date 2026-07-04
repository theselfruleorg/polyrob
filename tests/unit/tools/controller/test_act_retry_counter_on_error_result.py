"""B9/B18 (medium) — act() must count a RETURNED ActionResult(error=...) toward
max_retries, not just a raised exception.

act() reset the per-operation retry counter to 0 on every non-raising completion.
Most tools signal failure by RETURNING ActionResult(error=...) (they don't raise),
so the counter oscillated 0->1->0 and the max-retries guard never fired — a failing
action could be re-invoked unbounded.
"""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from tools.controller.service import Controller
from agents.task.agent.views import ActionResult


def _host(retry_limit=2):
    c = object.__new__(Controller)
    c.logger = logging.getLogger("retry-test")
    c._operation_attempts = {}
    c.session_id = "s"
    c.registry = MagicMock()
    c.registry.get_action.return_value = SimpleNamespace(tool="mytool")
    c.registry.execute_action = AsyncMock(return_value=ActionResult(error="boom"))
    c._get_retry_limit_for_tool = lambda tool: retry_limit
    return c


def _action():
    a = MagicMock()
    a.model_dump.return_value = {"myaction": {"x": 1}}
    return a


@pytest.mark.asyncio
async def test_returned_error_result_counts_toward_max_retries():
    c = _host(retry_limit=2)
    ctx = MagicMock()

    r1 = await c.act(_action(), execution_context=ctx)
    r2 = await c.act(_action(), execution_context=ctx)
    r3 = await c.act(_action(), execution_context=ctx)

    # Two real attempts, then the guard blocks the third (no reset on error result).
    assert c.registry.execute_action.await_count == 2
    assert r1.error == "boom" and r2.error == "boom"
    assert "Maximum retries" in (r3.error or "")


@pytest.mark.asyncio
async def test_success_result_resets_counter():
    c = _host(retry_limit=2)
    ctx = MagicMock()
    # First a failure, then a success — success must clear the counter.
    await c.act(_action(), execution_context=ctx)  # attempt 1 (error)
    c.registry.execute_action = AsyncMock(return_value=ActionResult(extracted_content="ok"))
    await c.act(_action(), execution_context=ctx)  # success -> reset
    assert c._operation_attempts.get("myaction:{'x': 1}", 0) == 0 or \
        all(v == 0 for v in c._operation_attempts.values())


@pytest.mark.asyncio
async def test_observe_error_result_runs_transform_and_post_hooks():
    """B21: transform + post tool-call hooks must observe error/timeout results too
    (audit/billing hooks previously only saw the success path)."""
    c = object.__new__(Controller)
    c.logger = logging.getLogger("hooks-err")
    seen_post = []
    seen_xform = []
    c.register_post_tool_call_hook(
        lambda name, params, result, ctx: seen_post.append((name, result.error)))
    def _xform(name, params, result, ctx):
        seen_xform.append(name)
        return result
    c.register_transform_tool_result_hook(_xform)
    err = ActionResult(error="boom")
    out = await c._observe_error_result("myaction", {"x": 1}, err, None)
    assert seen_xform == ["myaction"]
    assert seen_post and seen_post[0] == ("myaction", "boom")
    assert out.error == "boom"
