"""Tests for cli.ui.state.SessionState — update() and poll()."""

import pytest
from unittest.mock import MagicMock

from cli.ui.events import (
    ErrorEvent,
    Info,
    IterationDone,
    LLMCall,
    SessionDone,
    SessionStart,
    Step,
    ToolExec,
)
from cli.ui.state import SessionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state() -> SessionState:
    return SessionState()


def _make_mm(
    ctx_percent: float = 42.0,
    total_tokens: int = 1024,
    max_input_tokens: int = 8192,
    compaction_count: int = 2,
) -> MagicMock:
    """Build a fake message_manager with the four attributes poll() reads."""
    mm = MagicMock()
    mm.get_context_usage_percent.return_value = ctx_percent
    mm.history = MagicMock()
    mm.history.total_tokens = total_tokens
    mm.max_input_tokens = max_input_tokens
    mm._compaction_count = compaction_count
    return mm


def _make_agent(mm: MagicMock) -> MagicMock:
    agent = MagicMock()
    agent.message_manager = mm
    return agent


# ---------------------------------------------------------------------------
# Construction defaults
# ---------------------------------------------------------------------------


def test_initial_state_defaults():
    s = _make_state()
    assert s.model == ""
    assert s.provider == ""
    assert s.status == "starting"
    assert s.tokens_in == 0
    assert s.tokens_out == 0
    assert s.tokens_total == 0
    assert s.cost_estimate_total == 0.0
    assert s.step == 0
    assert s.tool_calls == 0
    assert s.errors == 0
    assert s.ctx_percent == 0.0
    assert s.ctx_tokens == 0
    assert s.ctx_max == 0
    assert s.compactions == 0


# ---------------------------------------------------------------------------
# update() — event accumulation
# ---------------------------------------------------------------------------


def test_update_session_start_sets_model():
    s = _make_state()
    s.update(SessionStart(model_name="gpt-5", agent_id="x"))
    assert s.model == "gpt-5"
    # Feed events no longer drive status — the TurnLifecycle (turn seam) owns it.
    # A SessionStart from any session must not flip the bar to "running".
    assert s.status == "starting"  # unchanged by the feed


def test_update_session_start_empty_model_not_clobber():
    """An empty model_name in SessionStart should not blank an existing model."""
    s = _make_state()
    s.model = "previous-model"
    s.update(SessionStart(model_name=""))
    assert s.model == "previous-model"


def test_update_llm_call_accumulates_tokens():
    s = _make_state()
    s.update(LLMCall(prompt_tokens=100, completion_tokens=50, token_count=150,
                     cost_estimate=0.001))
    s.update(LLMCall(prompt_tokens=200, completion_tokens=100, token_count=300,
                     cost_estimate=0.002))
    assert s.tokens_in == 300
    assert s.tokens_out == 150
    assert s.tokens_total == 450
    assert s.cost_estimate_total == pytest.approx(0.003)


def test_update_llm_call_none_tokens_not_accumulated():
    """None token values must not increment the counters."""
    s = _make_state()
    s.update(LLMCall(prompt_tokens=None, completion_tokens=None, token_count=None))
    assert s.tokens_in == 0
    assert s.tokens_out == 0
    assert s.tokens_total == 0


def test_update_llm_call_none_cost_not_accumulated():
    s = _make_state()
    s.update(LLMCall(cost_estimate=None))
    assert s.cost_estimate_total == 0.0


def test_update_llm_call_sets_provider_once():
    """provider is set from the first LLMCall with a non-empty provider."""
    s = _make_state()
    s.update(LLMCall(provider="anthropic"))
    s.update(LLMCall(provider="openai"))  # should not overwrite
    assert s.provider == "anthropic"


def test_update_llm_call_sets_model_if_not_set():
    s = _make_state()
    s.update(LLMCall(model_name="claude-opus-4"))
    assert s.model == "claude-opus-4"


def test_update_step_advances_step():
    s = _make_state()
    s.update(Step(step=3))
    assert s.step == 3


def test_update_step_max_wins():
    """step counter takes the maximum, not the latest."""
    s = _make_state()
    s.update(Step(step=5))
    s.update(Step(step=2))  # lower — should not regress
    assert s.step == 5


def test_update_tool_exec_increments_count():
    s = _make_state()
    s.update(ToolExec())
    s.update(ToolExec())
    assert s.tool_calls == 2


def test_last_step_sub_agent_tracks_step_identity():
    """tool_execution carries no agent identity, so the renderer suppresses
    sub-agent tool lines by correlating to the most recent Step's identity."""
    from cli.ui.events import AgentRegistration

    s = _make_state()
    assert s.last_step_sub_agent is False
    # Register the main agent.
    s.update(AgentRegistration(agent_id="main_1", agent_name="rob"))
    # A main-agent step.
    s.update(Step(step=1, raw={"data": {"agent_name": "rob"}}))
    assert s.last_step_sub_agent is False
    # A sub-agent step flips the flag.
    s.update(Step(step=2, raw={"data": {"agent_name": "leaf_42"}}))
    assert s.last_step_sub_agent is True
    # Back to a main-agent step resets it.
    s.update(Step(step=3, raw={"data": {"agent_name": "rob"}}))
    assert s.last_step_sub_agent is False


def test_update_error_event_increments_count_but_not_status():
    s = _make_state()
    s.update(ErrorEvent(error_message="boom"))
    assert s.errors == 1
    # Status is owned by the TurnLifecycle (turn seam), not driven by feed events.
    assert s.status == "starting"  # unchanged by the feed


def test_update_iteration_done_does_not_change_status():
    s = _make_state()
    s.update(IterationDone(is_done=True))
    assert s.status == "starting"  # lifecycle settles on respond() return, not a feed event


def test_update_iteration_done_not_done_no_status_change():
    s = _make_state()
    s.status = "working"
    s.update(IterationDone(is_done=False))
    assert s.status == "working"


def test_update_session_done_does_not_change_status():
    s = _make_state()
    s.update(SessionDone(success=True))
    assert s.status == "starting"  # feed no longer sets the status word
    s.update(SessionDone(success=False))
    assert s.status == "starting"


def test_update_info_event_no_state_change():
    """Info events should not modify any numeric counters."""
    s = _make_state()
    before_tools = s.tool_calls
    before_errors = s.errors
    s.update(Info(type="status"))
    assert s.tool_calls == before_tools
    assert s.errors == before_errors


# ---------------------------------------------------------------------------
# poll()
# ---------------------------------------------------------------------------


def test_poll_reads_ctx_percent():
    s = _make_state()
    agent = _make_agent(_make_mm(ctx_percent=75.5))
    s.poll(agent)
    assert s.ctx_percent == pytest.approx(75.5)


def test_poll_reads_total_tokens():
    s = _make_state()
    agent = _make_agent(_make_mm(total_tokens=2048))
    s.poll(agent)
    assert s.ctx_tokens == 2048


def test_poll_reads_max_input_tokens():
    s = _make_state()
    agent = _make_agent(_make_mm(max_input_tokens=16384))
    s.poll(agent)
    assert s.ctx_max == 16384


def test_poll_reads_compaction_count():
    s = _make_state()
    agent = _make_agent(_make_mm(compaction_count=3))
    s.poll(agent)
    assert s.compactions == 3


def test_poll_missing_attribute_does_not_raise():
    """poll() must be fail-safe — a broken message_manager must not crash."""
    s = _make_state()
    bad_agent = MagicMock()
    bad_agent.message_manager.get_context_usage_percent.side_effect = AttributeError("nope")
    bad_agent.message_manager.history = MagicMock(spec=[])  # no total_tokens
    # Must not raise
    s.poll(bad_agent)


def test_poll_completely_missing_message_manager():
    """poll() with an agent that has no message_manager must not raise."""
    s = _make_state()
    agent = MagicMock(spec=["nothing"])  # no message_manager attribute
    s.poll(agent)  # should silently no-op


# ---------------------------------------------------------------------------
# elapsed()
# ---------------------------------------------------------------------------


def test_elapsed_increases_over_time():
    import time
    s = _make_state()
    t0 = s.elapsed()
    time.sleep(0.05)
    t1 = s.elapsed()
    assert t1 > t0


def test_elapsed_starts_near_zero():
    s = _make_state()
    assert s.elapsed() < 1.0  # fresh state: well under 1 second


# ---------------------------------------------------------------------------
# poll_usage() — min_interval throttling
# ---------------------------------------------------------------------------


def test_poll_usage_min_interval_throttles_scan(tmp_path):
    t = {"now": 100.0}
    state = SessionState(clock=lambda: t["now"])
    usage_dir = tmp_path / "data" / "llm_usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "llm_usage_1.json").write_text(
        '{"prompt_tokens": 10, "completion_tokens": 5, "token_count": 15, "cost_estimate": 0.01}'
    )
    state.poll_usage(tmp_path, min_interval=0.5)
    assert state.tokens_total == 15

    # A new file arrives 0.1s later — inside the interval, scan must be skipped.
    (usage_dir / "llm_usage_2.json").write_text(
        '{"prompt_tokens": 10, "completion_tokens": 5, "token_count": 15, "cost_estimate": 0.01}'
    )
    t["now"] = 100.1
    state.poll_usage(tmp_path, min_interval=0.5)
    assert state.tokens_total == 15  # unchanged: throttled

    # Past the interval the scan runs and picks the file up.
    t["now"] = 100.7
    state.poll_usage(tmp_path, min_interval=0.5)
    assert state.tokens_total == 30


def test_poll_usage_default_is_unthrottled(tmp_path):
    t = {"now": 100.0}
    state = SessionState(clock=lambda: t["now"])
    usage_dir = tmp_path / "data" / "llm_usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "llm_usage_1.json").write_text(
        '{"prompt_tokens": 1, "completion_tokens": 1, "token_count": 2, "cost_estimate": 0.0}'
    )
    state.poll_usage(tmp_path)
    (usage_dir / "llm_usage_2.json").write_text(
        '{"prompt_tokens": 1, "completion_tokens": 1, "token_count": 2, "cost_estimate": 0.0}'
    )
    state.poll_usage(tmp_path)  # same instant, no interval → still scans
    assert state.tokens_total == 4
