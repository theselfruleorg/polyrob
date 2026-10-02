"""W1.1 (console transformation 070) — tool completion telemetry runs on every path.

``Controller.act`` used to write ``tool_execution`` / ``tool_result`` only inside an
``if laminar_available:`` branch. ``laminar`` is not a dependency, so that branch
never ran and the feed held ``tool_started`` only: the console showed every tool
row as "Ran a tool 0:00" forever. These tests pin that the ONE surviving execution
path records the completion on success and on every failure shape, and that a
telemetry failure is visible (WARNING) without changing the action result.
"""
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from tools.controller.service import Controller
from tools.controller.types import ActionResult

REPO_ROOT = Path(__file__).resolve().parents[4]


def _host(execute):
    """A Controller with a stub registry action and a stub orchestrator."""
    captured = {"exec": [], "feed": []}

    def _capture_tool_execution(**kwargs):
        captured["exec"].append(kwargs)

    def _add_to_feed(session_id, event_type, data):
        captured["feed"].append((event_type, data))

    c = object.__new__(Controller)
    c.logger = logging.getLogger("test_tool_completion_telemetry")
    c._operation_attempts = {}
    c.session_id = "s1"
    c.registry = MagicMock()
    c.registry.get_action.return_value = SimpleNamespace(tool="filesystem")
    c.registry.execute_action = execute
    c._get_retry_limit_for_tool = lambda tool: 5
    c.orchestrator = SimpleNamespace(
        telemetry_manager=SimpleNamespace(capture_tool_execution=_capture_tool_execution),
        session_manager=SimpleNamespace(add_to_feed=_add_to_feed),
        current_step=2,
    )
    return c, captured


def _action(name="read_file"):
    a = MagicMock()
    a.model_dump.return_value = {name: {"path": "README.md"}}
    a._tool_call_id = "call_1"
    return a


def _ctx():
    return SimpleNamespace(
        browser_context=None, sensitive_data={}, available_file_paths=[],
        session_id="s1", user_id="u1",
    )


def _tool_results(captured):
    return [d for t, d in captured["feed"] if t == "tool_result"]


@pytest.mark.asyncio
async def test_success_writes_tool_execution_and_tool_result():
    c, captured = _host(AsyncMock(return_value=ActionResult(extracted_content="# README")))
    out = await c.act(_action(), execution_context=_ctx())

    assert out.error is None
    assert len(captured["exec"]) == 1
    assert captured["exec"][0]["success"] is True
    assert captured["exec"][0]["action_name"] == "read_file"
    assert captured["exec"][0]["call_id"] == "call_1"
    results = _tool_results(captured)
    assert len(results) == 1
    assert results[0]["success"] is True
    assert results[0]["narration"]


@pytest.mark.asyncio
async def test_returned_error_is_a_failed_completion():
    c, captured = _host(AsyncMock(return_value=ActionResult(error="x")))
    out = await c.act(_action(), execution_context=_ctx())

    assert out.error == "x"
    assert len(captured["exec"]) == 1
    assert captured["exec"][0]["success"] is False
    results = _tool_results(captured)
    assert len(results) == 1
    assert results[0]["success"] is False


@pytest.mark.asyncio
async def test_raised_error_is_a_failed_completion():
    c, captured = _host(AsyncMock(side_effect=RuntimeError("kaput")))
    out = await c.act(_action(), execution_context=_ctx())

    assert out.error and "read_file" in out.error
    assert len(captured["exec"]) == 1
    assert captured["exec"][0]["success"] is False
    results = _tool_results(captured)
    assert len(results) == 1
    assert results[0]["success"] is False


@pytest.mark.asyncio
async def test_not_implemented_keeps_the_detail_text():
    c, captured = _host(AsyncMock(side_effect=NotImplementedError("later")))
    out = await c.act(_action(), execution_context=_ctx())

    assert out.error and "NotImplementedError in action read_file" in out.error
    assert "later" in out.error
    assert len(captured["exec"]) == 1
    assert captured["exec"][0]["success"] is False
    assert _tool_results(captured)[0]["success"] is False


@pytest.mark.asyncio
async def test_telemetry_failure_logs_warning(caplog):
    c, captured = _host(AsyncMock(return_value=ActionResult(extracted_content="ok")))

    def _boom(**kwargs):
        raise ValueError("disk gone")

    c.orchestrator.telemetry_manager.capture_tool_execution = _boom
    with caplog.at_level(logging.DEBUG, logger="test_tool_completion_telemetry"):
        out = await c.act(_action(), execution_context=_ctx())

    assert out.extracted_content == "ok" and out.error is None
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "tool telemetry" in r.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "read_file" in msg and "ValueError" in msg
    # The exception text (which may carry params) is not logged.
    assert "disk gone" not in msg


def test_no_laminar_reference_left():
    src = (REPO_ROOT / "tools/controller/execution.py").read_text()
    assert "laminar" not in src.lower()


@pytest.mark.asyncio
async def test_retry_limit_is_a_failed_completion():
    """multi_act already wrote tool_started; the retry-limit refusal must close it."""
    c, captured = _host(AsyncMock(return_value=ActionResult(error="x")))
    c._get_retry_limit_for_tool = lambda tool: 1
    await c.act(_action(), execution_context=_ctx())
    out = await c.act(_action(), execution_context=_ctx())

    assert out.error and "Maximum retries" in out.error
    results = _tool_results(captured)
    assert len(results) == 2, "retry-limit return wrote no tool_result"
    assert results[1]["success"] is False
    assert captured["exec"][1]["call_id"] == "call_1"


@pytest.mark.asyncio
async def test_timeout_is_a_failed_completion(monkeypatch):
    """A tool that outlives its timeout is cancelled inside multi_act; the
    tool_started row must still get its completion (same call_id)."""
    import asyncio

    async def _slow(*a, **k):
        await asyncio.sleep(5)

    c, captured = _host(_slow)
    started = []
    c._capture_tool_started = lambda **kw: started.append(kw)
    from agents.task.constants import TimeoutConfig
    monkeypatch.setattr(TimeoutConfig, "get_tool_timeout", staticmethod(lambda t: 0.01))
    out = await c.multi_act([_action()], execution_context=_ctx())

    assert out and out[0].error and "timed out" in out[0].error
    results = _tool_results(captured)
    assert len(results) == 1, captured["feed"]
    assert results[0]["success"] is False
    assert captured["exec"][0]["call_id"] == started[0]["call_id"]
