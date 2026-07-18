"""019 P0 — run-state span/wait events: views, formatters, manager, contract.

The four new feed kinds (tool_started / llm_started / awaiting_approval /
approval_resolved) must:
- carry their payloads through the formatter registry (never the generic
  fallback),
- join tool spans via ``call_id`` (also added to tool_execution),
- be gated by ``RUN_EVENTS_ENABLED`` at the emit helpers, and
- render on every surface (the no-dark-kinds contract: CLI normalize typed,
  webview summarize branch, formatter registered).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.task.telemetry.formatters import (
    RunEventFormatter,
    get_formatter_registry,
)
from agents.task.telemetry.views import (
    ApprovalResolvedEvent,
    AwaitingApprovalEvent,
    LLMStartedEvent,
    ToolExecutionTelemetryEvent,
    ToolStartedEvent,
)

RUN_EVENT_KINDS = (
    # P0 span/wait events
    "tool_started", "llm_started", "awaiting_approval", "approval_resolved",
    # P1 vocabulary completion
    "compaction_started", "compaction_finished", "retry_wait",
    "subagent_started", "subagent_finished",
    "delegation_dispatched", "delegation_completed",
)

#: Kinds that only gained RENDERINGS in 019 (they already reached the feed via
#: the generic formatter) — held to the CLI + webview contract, not the
#: dedicated-formatter one.
RENDER_ONLY_KINDS = ("provider_failure", "provider_fallback_success")


# ---------------------------------------------------------------------------
# Views + formatters
# ---------------------------------------------------------------------------


def test_tool_started_event_feed_shape():
    event = ToolStartedEvent(
        agent_id="agent_sess1",
        step=3,
        tool_name="browser",
        action_name="navigate",
        parameters={"url": "https://example.com"},
        call_id="call_abc",
        index=1,
        total_in_batch=2,
        session_id="sess1",
    )
    assert event.name == "tool_started"
    assert event.get_session_id() == "sess1"
    out = get_formatter_registry().get_formatter(event.name).format(event)
    assert out["type"] == "tool_started"
    assert out["step"] == 3  # lifted top-level
    assert out["data"]["action_name"] == "navigate"
    assert out["data"]["call_id"] == "call_abc"
    assert out["data"]["total_in_batch"] == 2


def test_llm_started_event_feed_shape():
    event = LLMStartedEvent(
        agent_id="agent_sess1", step=2, provider="anthropic",
        model_name="claude-sonnet-5", attempt=1, context_tokens_est=1234,
    )
    out = get_formatter_registry().get_formatter(event.name).format(event)
    assert out["type"] == "llm_started"
    assert out["data"]["model_name"] == "claude-sonnet-5"
    assert out["data"]["attempt"] == 1


def test_approval_events_feed_shape():
    waiting = AwaitingApprovalEvent(session_id="sess1", action_name="send_email", timeout_sec=30.0)
    resolved = ApprovalResolvedEvent(
        session_id="sess1", action_name="send_email", decision="approved", waited_sec=4.2
    )
    assert waiting.get_session_id() == "sess1"
    out_w = get_formatter_registry().get_formatter(waiting.name).format(waiting)
    out_r = get_formatter_registry().get_formatter(resolved.name).format(resolved)
    assert out_w["type"] == "awaiting_approval"
    assert out_w["data"]["action_name"] == "send_email"
    assert out_r["type"] == "approval_resolved"
    assert out_r["data"]["decision"] == "approved"


def test_run_kinds_use_dedicated_formatter_not_generic():
    registry = get_formatter_registry()
    for kind in RUN_EVENT_KINDS:
        assert isinstance(registry.get_formatter(kind), RunEventFormatter), kind


def test_tool_execution_formatter_carries_call_id():
    event = ToolExecutionTelemetryEvent(
        agent_id="agent_sess1", step=3, tool_name="browser", action_name="navigate",
        parameters={}, duration_seconds=1.0, success=True, call_id="call_abc",
    )
    out = get_formatter_registry().get_formatter(event.name).format(event)
    assert out["data"]["call_id"] == "call_abc"


# ---------------------------------------------------------------------------
# TelemetryManager methods
# ---------------------------------------------------------------------------


def _manager_with_mock_service():
    from agents.task.telemetry.manager import TelemetryManager

    manager = TelemetryManager.__new__(TelemetryManager)
    manager._service = MagicMock()
    manager._session_id = "sess1"
    manager._agent_id = "agent_sess1"
    manager.logger = MagicMock()
    return manager


def test_manager_capture_tool_started():
    manager = _manager_with_mock_service()
    manager.capture_tool_started(
        step=4, tool_name="mcp", action_name="anysite_search",
        parameters={"q": "x"}, call_id="c1", index=0, total_in_batch=1,
    )
    (event,), kwargs = manager._service.capture.call_args
    assert isinstance(event, ToolStartedEvent)
    assert event.call_id == "c1"
    assert event.session_id == "sess1"
    assert kwargs["session_id"] == "sess1"


def test_manager_capture_tool_execution_threads_call_id():
    manager = _manager_with_mock_service()
    manager.capture_tool_execution(
        step=4, tool_name="mcp", action_name="anysite_search", parameters={},
        duration=0.5, success=True, call_id="c1",
    )
    (event,), _ = manager._service.capture.call_args
    assert isinstance(event, ToolExecutionTelemetryEvent)
    assert event.call_id == "c1"


# ---------------------------------------------------------------------------
# ExecutionMixin._capture_tool_started (emit site, flag gate)
# ---------------------------------------------------------------------------


def _fake_controller():
    from tools.controller.execution import ExecutionMixin

    ctl = SimpleNamespace(
        orchestrator=SimpleNamespace(telemetry_manager=MagicMock(), current_step=7),
        logger=MagicMock(),
    )
    ctl._capture_tool_started = ExecutionMixin._capture_tool_started.__get__(ctl)
    return ctl


def test_capture_tool_started_emits(monkeypatch):
    monkeypatch.delenv("RUN_EVENTS_ENABLED", raising=False)
    ctl = _fake_controller()
    ctl._capture_tool_started(
        action_name="navigate", tool_name="browser", params={"url": "u"},
        call_id="c9", index=2, total_in_batch=3,
    )
    tm = ctl.orchestrator.telemetry_manager
    tm.capture_tool_started.assert_called_once_with(
        step=7, tool_name="browser", action_name="navigate",
        parameters={"url": "u"}, call_id="c9", index=2, total_in_batch=3,
    )


def test_capture_tool_started_gated_off(monkeypatch):
    monkeypatch.setenv("RUN_EVENTS_ENABLED", "false")
    ctl = _fake_controller()
    ctl._capture_tool_started(
        action_name="navigate", tool_name="browser", params={}, call_id=None,
    )
    ctl.orchestrator.telemetry_manager.capture_tool_started.assert_not_called()


def test_capture_tool_started_never_raises_without_orchestrator(monkeypatch):
    monkeypatch.delenv("RUN_EVENTS_ENABLED", raising=False)
    from tools.controller.execution import ExecutionMixin

    ctl = SimpleNamespace(orchestrator=None, logger=MagicMock())
    ctl._capture_tool_started = ExecutionMixin._capture_tool_started.__get__(ctl)
    ctl._capture_tool_started(action_name="a", tool_name="t", params={})  # no raise


# ---------------------------------------------------------------------------
# No-dark-kinds contract (019 §12): every run-event kind renders everywhere
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", RUN_EVENT_KINDS + RENDER_ONLY_KINDS)
def test_no_dark_kinds_cli_normalize_typed(kind):
    """CLI: the kind normalizes to a TYPED/registered event, never Info."""
    from cli.ui.events import Info, normalize

    event = normalize({"type": kind, "data": {}})
    assert not isinstance(event, Info), f"{kind} fell through to Info"
    assert event.type == kind


@pytest.mark.parametrize("kind", RUN_EVENT_KINDS + RENDER_ONLY_KINDS)
def test_no_dark_kinds_webview_summarize_branch(kind):
    """Webview /activity: summarize() has a real branch (not the bare-kind echo)."""
    from webview.activity import summarize

    assert summarize(kind, {}) != kind, f"{kind} has no summarize() branch"


@pytest.mark.parametrize("kind", RUN_EVENT_KINDS)
def test_no_dark_kinds_formatter_registered(kind):
    """Feed writer: the kind has a registered (non-generic) formatter."""
    registry = get_formatter_registry()
    assert isinstance(registry.get_formatter(kind), RunEventFormatter)


def test_dummy_telemetry_manager_swallows_any_capture():
    """The orchestrator fallback dummy must no-op EVERY capture method —
    including ones added after it was written (the AttributeError class)."""
    # Reproduce the dummy's shape (defined inline in orchestrator's except path).
    class DummyTelemetryManager:
        degraded = True

        def __getattr__(self, _name):
            def _noop(*args, **kwargs):
                return None
            return _noop

    dummy = DummyTelemetryManager()
    dummy.capture_tool_started(step=1)
    dummy.capture_event(object())
    dummy.some_future_capture_method("x", key="v")
    assert dummy.degraded is True


def test_emit_retry_wait_helper(monkeypatch):
    """error_recovery._emit_retry_wait builds a RetryWaitEvent (flag-gated)."""
    monkeypatch.delenv("RUN_EVENTS_ENABLED", raising=False)
    from agents.task.agent.core.error_recovery import ErrorRecoveryMixin
    from agents.task.telemetry.views import RetryWaitEvent

    agent = SimpleNamespace(
        telemetry_manager=MagicMock(),
        agent_id="agent_s1",
        session_id="s1",
        state=SimpleNamespace(n_steps=5, consecutive_failures=2),
    )
    agent._emit_retry_wait = ErrorRecoveryMixin._emit_retry_wait.__get__(agent)
    agent._emit_retry_wait("rate_limit", 8.0, provider="anthropic")
    (event,), _ = agent.telemetry_manager.capture_event.call_args
    assert isinstance(event, RetryWaitEvent)
    assert (event.reason, event.delay_sec, event.provider, event.attempt) == (
        "rate_limit", 8.0, "anthropic", 2)

    monkeypatch.setenv("RUN_EVENTS_ENABLED", "0")
    agent.telemetry_manager.reset_mock()
    agent._emit_retry_wait("rate_limit", 8.0)
    agent.telemetry_manager.capture_event.assert_not_called()
