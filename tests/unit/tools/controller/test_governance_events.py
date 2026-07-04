"""Governance events → durable event log (telemetry audit 2026-07-04).

Tool denials/timeouts were logged but never telemetered. ExecutionMixin now emits
tool_denied/tool_timeout to the event log, tenant-scoped from execution_context.
"""
from types import SimpleNamespace

from tools.controller.execution import ExecutionMixin


def test_emit_governance_event_records(tmp_path, monkeypatch):
    import agents.task.telemetry.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    test_log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: test_log)

    mixin = ExecutionMixin.__new__(ExecutionMixin)
    ctx = SimpleNamespace(user_id="u1", session_id="s1")

    mixin._emit_governance_event("tool_denied", ctx, action="x402_pay", reason="denylist")
    mixin._emit_governance_event("tool_timeout", ctx, action="browser", timeout_s=30)

    denied = test_log.query(kind="tool_denied")
    assert denied and denied[0]["user_id"] == "u1"
    assert denied[0]["attrs"]["action"] == "x402_pay"
    assert denied[0]["attrs"]["reason"] == "denylist"
    assert test_log.query(kind="tool_timeout")[0]["attrs"]["timeout_s"] == 30


def test_emit_governance_event_fail_open_without_context():
    mixin = ExecutionMixin.__new__(ExecutionMixin)
    # No context, no configured log — must not raise.
    mixin._emit_governance_event("tool_denied", None, action="a")
