"""019 P0 — CLI rendering of the run-state span/wait events.

Covers: typed normalization, the tool-span pairing rule (start line at
dispatch, only the result line at the paired completion, legacy two-line form
for unpaired completions), approval notices, /quiet gating, the SessionState
current-activity fields, and the status-bar activity segment.
"""
import io

from cli.ui.events import (
    ApprovalDecision,
    ApprovalPending,
    LLMStarted,
    ToolExec,
    ToolStarted,
    normalize,
)
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState
from cli.ui import statusbar


def _started_dict(call_id="c1", action="navigate", step=3):
    return {
        "type": "tool_started",
        "step": step,
        "data": {
            "tool_name": "browser",
            "action_name": action,
            "parameters": {"url": "https://x.test"},
            "call_id": call_id,
            "index": 0,
            "total_in_batch": 1,
        },
    }


def _exec_dict(call_id="c1", action="navigate", step=3, success=True):
    return {
        "type": "tool_execution",
        "step": step,
        "data": {
            "tool_name": "browser",
            "action_name": action,
            "success": success,
            "duration_seconds": 1.2,
            "parameters": {"url": "https://x.test"},
            "result_preview": "ok",
            "call_id": call_id,
        },
    }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_tool_started_typed():
    event = normalize(_started_dict())
    assert isinstance(event, ToolStarted)
    assert event.action_name == "navigate"
    assert event.call_id == "c1"
    assert event.step == 3


def test_normalize_tool_exec_carries_call_id():
    event = normalize(_exec_dict())
    assert isinstance(event, ToolExec)
    assert event.call_id == "c1"


def test_normalize_llm_started_and_approvals():
    llm = normalize({"type": "llm_started", "step": 2,
                     "data": {"provider": "anthropic", "model_name": "m", "attempt": 1}})
    assert isinstance(llm, LLMStarted) and llm.model_name == "m"
    pend = normalize({"type": "awaiting_approval",
                      "data": {"action_name": "send_email", "timeout_sec": 30}})
    assert isinstance(pend, ApprovalPending) and pend.timeout_sec == 30.0
    dec = normalize({"type": "approval_resolved",
                     "data": {"action_name": "send_email", "decision": "approved",
                              "waited_sec": 4.5}})
    assert isinstance(dec, ApprovalDecision) and dec.decision == "approved"


# ---------------------------------------------------------------------------
# Pairing: → at start, only ✓ at paired completion, legacy form unpaired
# ---------------------------------------------------------------------------


def _renderer():
    buf = io.StringIO()
    state = SessionState()
    renderer = PlainRenderer(state=state, stream=buf)
    return renderer, state, buf


def _feed(renderer, state, feed_dict):
    event = normalize(feed_dict)
    state.update(event)
    renderer.on_event(event)


def test_paired_span_prints_one_call_line_and_one_result():
    renderer, state, buf = _renderer()
    _feed(renderer, state, _started_dict())
    _feed(renderer, state, _exec_dict())
    out = buf.getvalue()
    assert out.count("→ navigate(") == 1  # printed at START, not repeated
    assert out.count("✓ navigate") == 1
    # the → line arrived before the ✓ line
    assert out.index("→ navigate(") < out.index("✓ navigate")


def test_unpaired_completion_keeps_legacy_two_line_form():
    renderer, state, buf = _renderer()
    _feed(renderer, state, _exec_dict(call_id=None))
    out = buf.getvalue()
    assert out.count("→ navigate(") == 1
    assert out.count("✓ navigate") == 1


def test_completion_without_matching_start_id_falls_back():
    renderer, state, buf = _renderer()
    _feed(renderer, state, _started_dict(call_id="c1"))
    _feed(renderer, state, _exec_dict(call_id="OTHER"))
    out = buf.getvalue()
    # start line for c1 + legacy pair for OTHER
    assert out.count("→ navigate(") == 2
    assert out.count("✓ navigate") == 1


def test_quiet_mutes_tool_start_lines_but_not_approval():
    renderer, state, buf = _renderer()
    renderer.show_tools = False  # /quiet
    _feed(renderer, state, _started_dict())
    _feed(renderer, state, _exec_dict())
    _feed(renderer, state, {"type": "awaiting_approval",
                            "data": {"action_name": "send_email"}})
    out = buf.getvalue()
    assert "navigate" not in out
    assert "awaiting approval: send_email" in out


def test_approval_notices_render():
    renderer, state, buf = _renderer()
    _feed(renderer, state, {"type": "awaiting_approval",
                            "data": {"action_name": "send_email", "timeout_sec": 30}})
    _feed(renderer, state, {"type": "approval_resolved",
                            "data": {"action_name": "send_email", "decision": "approved",
                                     "waited_sec": 4.0}})
    out = buf.getvalue()
    assert "awaiting approval: send_email" in out
    assert "/pending" in out
    assert "approved" in out


def test_verbose_prints_start_trace_lines():
    renderer, state, buf = _renderer()
    renderer.verbose = True
    _feed(renderer, state, _started_dict())
    _feed(renderer, state, {"type": "llm_started", "step": 3,
                            "data": {"provider": "p", "model_name": "m"}})
    out = buf.getvalue()
    assert "[tool] start browser/navigate" in out
    assert "[llm] start m" in out


# ---------------------------------------------------------------------------
# SessionState current-activity + status bar segment
# ---------------------------------------------------------------------------


def test_state_activity_lifecycle():
    now = [100.0]
    state = SessionState(clock=lambda: now[0])
    state.update(normalize(_started_dict()))
    assert state.current_activity_kind == "tool"
    assert state.current_activity == "navigate"
    now[0] = 143.0
    assert state.activity_elapsed() == 43.0
    state.update(normalize(_exec_dict()))
    assert state.current_activity_kind == ""
    assert state.activity_elapsed() == 0.0


def test_state_approval_survives_step_boundary():
    state = SessionState()
    state.update(normalize({"type": "awaiting_approval",
                            "data": {"action_name": "send_email"}}))
    assert state.current_activity_kind == "approval"
    # a step event clears tool/llm activity but NOT a blocking approval
    state.update(normalize({"type": "step", "step": 4, "data": {}}))
    assert state.current_activity_kind == "approval"
    state.update(normalize({"type": "approval_resolved",
                            "data": {"action_name": "send_email",
                                     "decision": "approved"}}))
    assert state.current_activity_kind == ""


def test_statusbar_shows_inflight_tool_with_clock():
    now = [100.0]
    state = SessionState(clock=lambda: now[0])
    token = state.lifecycle.begin_turn()
    state.update(normalize(_started_dict()))
    now[0] = 143.0
    line = statusbar.status_text(state)
    assert "→navigate 43s" in line
    state.lifecycle.end_turn(token)
    assert "navigate" not in statusbar.status_text(state)  # idle → no lingering


def test_statusbar_shows_thinking_and_approval():
    state = SessionState()
    state.lifecycle.begin_turn()
    state.update(normalize({"type": "llm_started", "step": 1,
                            "data": {"provider": "p", "model_name": "m"}}))
    assert "thinking" in statusbar.status_text(state)
    state.update(normalize({"type": "awaiting_approval",
                            "data": {"action_name": "send_email"}}))
    assert "approval: send_email /pending" in statusbar.status_text(state)


def test_statusbar_falls_back_to_last_tool_without_span_events():
    """RUN_EVENTS_ENABLED=off / old feeds: the legacy last-tool segment stands."""
    state = SessionState()
    state.lifecycle.begin_turn()
    state.update(normalize(_exec_dict(call_id=None)))
    assert "→navigate" in statusbar.status_text(state)


# ---------------------------------------------------------------------------
# 019 P1: registry-rendered run-state kinds (compaction/retry/subagent/
# delegation/provider failover) — layers + state activity
# ---------------------------------------------------------------------------


def test_subagent_and_delegation_lines_render_by_default():
    renderer, state, buf = _renderer()
    _feed(renderer, state, {"type": "subagent_started",
                            "data": {"goal_preview": "research pricing"}})
    _feed(renderer, state, {"type": "subagent_finished",
                            "data": {"ok": True, "duration_seconds": 12.0}})
    _feed(renderer, state, {"type": "delegation_dispatched",
                            "data": {"delegation_id": "deleg_0001",
                                     "goal_preview": "long job"}})
    _feed(renderer, state, {"type": "delegation_completed",
                            "data": {"delegation_id": "deleg_0001",
                                     "status": "completed",
                                     "duration_seconds": 90.0}})
    out = buf.getvalue()
    assert "sub-agent started: research pricing" in out
    assert "sub-agent finished ✓ · 12s" in out
    assert "delegation deleg_0001 dispatched: long job" in out
    assert "delegation deleg_0001 completed · 90s" in out


def test_provider_failover_lines_render_by_default():
    renderer, state, buf = _renderer()
    _feed(renderer, state, {"type": "provider_failure",
                            "data": {"failed_provider": "anthropic",
                                     "error_type": "RateLimitError",
                                     "fallback_provider": "openai"}})
    _feed(renderer, state, {"type": "provider_fallback_success",
                            "data": {"original_provider": "anthropic",
                                     "fallback_provider": "openai"}})
    out = buf.getvalue()
    assert "provider anthropic failed (RateLimitError) — trying openai" in out
    assert "provider fallback: anthropic → openai ✓" in out


def test_compaction_and_retry_are_trace_layer():
    """Default view: bar-only (state activity); scrollback line only under /verbose."""
    renderer, state, buf = _renderer()
    _feed(renderer, state, {"type": "compaction_started", "data": {"mode": "llm"}})
    _feed(renderer, state, {"type": "retry_wait",
                            "data": {"reason": "rate_limit", "delay_sec": 8.0,
                                     "attempt": 2}})
    assert buf.getvalue() == ""  # no scrollback by default
    # but the state activity IS set (bar shows it)
    assert state.current_activity_kind == "retrying"

    renderer.verbose = True
    _feed(renderer, state, {"type": "compaction_started", "data": {"mode": "llm"}})
    _feed(renderer, state, {"type": "retry_wait",
                            "data": {"reason": "rate_limit", "delay_sec": 8.0,
                                     "attempt": 2}})
    out = buf.getvalue()
    assert "[compact] start mode=llm" in out
    assert "[retry] rate_limit wait=8s attempt=2" in out


def test_compaction_activity_sets_and_clears_state():
    state = SessionState()
    state.update(normalize({"type": "compaction_started", "data": {"mode": "emergency"}}))
    assert state.current_activity_kind == "compacting"
    assert "emergency" in state.current_activity
    state.update(normalize({"type": "compaction_finished", "data": {"mode": "emergency"}}))
    assert state.current_activity_kind == ""


def test_statusbar_shows_compacting_and_retry():
    state = SessionState()
    state.lifecycle.begin_turn()
    state.update(normalize({"type": "compaction_started", "data": {"mode": "llm"}}))
    assert "compacting (llm)" in statusbar.status_text(state)
    state.update(normalize({"type": "compaction_finished", "data": {"mode": "llm"}}))
    state.update(normalize({"type": "retry_wait",
                            "data": {"reason": "rate_limit", "delay_sec": 8.0}}))
    assert "retry (rate_limit) 8s" in statusbar.status_text(state)


def test_paired_completion_closes_even_when_gate_flips():
    """Review-fix regression: a printed `→` start line must ALWAYS get its
    result line — even if _should_show_tool flips mid-flight (synchronous
    delegate_task sets last_step_sub_agent=True before its own completion)."""
    renderer, state, buf = _renderer()
    _feed(renderer, state, _started_dict(call_id="d1", action="delegate_task"))
    state.last_step_sub_agent = True  # the sub-agent's steps flipped the flag
    _feed(renderer, state, _exec_dict(call_id="d1", action="delegate_task"))
    out = buf.getvalue()
    assert out.count("→ delegate_task(") == 1
    assert out.count("✓ delegate_task") == 1  # closed, not orphaned
