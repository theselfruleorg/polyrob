"""Wired run-loop test for the RUN_BUDGET_USD gate (T1.1).

Fixture pattern copied from tests/unit/agents/test_verify_before_done_wired.py
(_build_run_loop_agent) — object.__new__(Agent) + pin only what run() touches.
"""
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import agents.task.agent.core.run_budget as rb
from agents.task.agent.service import Agent
from agents.task.agent.views import ActionResult, AgentHistoryList
from agents.task.session.execution import _result_session_status, _terminal_error_text


class _FakeTracker:
    db = None

    async def get_session_breakdown(self, session_id):
        return {"total_credits_charged": 0, "total_user_cost_usd": 0.0}


def _build_agent():
    a = object.__new__(Agent)
    a.logger = MagicMock()
    a._cancelled = False
    a.task = "test task"
    a.initial_actions = None
    a.generate_gif = False
    a.stall_timeout_seconds = None
    a.validate_output = False
    a._is_sub_agent = False
    a.agent_id = "agent-1"
    a.state = types.SimpleNamespace(
        n_steps=0, consecutive_failures=0, stopped=False, done=False, paused=False,
        reset_llm_errors=MagicMock(),
    )

    async def _fake_step(step_info=None):
        a.state.n_steps += 1
        a._last_result = [ActionResult(extracted_content="ok", is_done=False)]

    a.step = AsyncMock(side_effect=_fake_step)
    a._too_many_failures = MagicMock(return_value=False)
    a._handle_control_flags = AsyncMock(return_value=False)
    a._check_context_overflow = MagicMock(return_value=False)
    a._drain_user_messages = AsyncMock(return_value=[])
    a._maybe_spawn_background_review = MagicMock()
    a.telemetry_manager = MagicMock()
    a.orchestrator = MagicMock()
    a.orchestrator.browser_manager = None
    a.orchestrator.user_id = "u1"
    a.orchestrator.session_id = "sess-budget"
    a.message_manager = MagicMock()
    a.message_manager.get_token_count.return_value = 100
    a.message_manager.model_name = "test-model"
    a.task_context_manager = None
    a.usage_tracker = _FakeTracker()
    a.register_done_callback = None
    a.history = AgentHistoryList(history=[])
    a._last_result = None
    return a


def _rollup_sequence(values):
    seq = list(values)
    async def rollup(user_id, session_id=None, since=None, *, db=None):
        spent = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"api_cost_usd": spent, "credits": 0, "calls": 1}
    return rollup


@pytest.mark.asyncio
async def test_halts_before_next_step_when_over_budget(monkeypatch):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    monkeypatch.setattr(rb, "usage_rollup", _rollup_sequence([0.05, 0.25]))
    agent = _build_agent()
    await agent.run(max_steps=5, _continue_session=True)
    assert agent.step.await_count == 1          # step 1 ran; gate tripped before step 2
    assert agent.state.stopped is True
    assert _result_session_status(agent.history) == "error"  # honest status
    last = agent.history.history[-1].result[-1]
    assert rb.RUN_BUDGET_MARKER in (last.error or "")
    # Seam-level (final-review fix): execute_session's success-path branch
    # extracts this same text via _terminal_error_text so it reaches
    # task_agent_lite's `agent_result.get('error', ...)` instead of being
    # silently dropped ("Session failed: Unknown error").
    seam_error = _terminal_error_text(agent.history)
    assert seam_error is not None and rb.RUN_BUDGET_MARKER in seam_error


@pytest.mark.asyncio
async def test_already_over_budget_halts_before_any_step(monkeypatch):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    monkeypatch.setattr(rb, "usage_rollup", _rollup_sequence([0.50]))
    agent = _build_agent()
    await agent.run(max_steps=5, _continue_session=True)
    assert agent.step.await_count == 0


@pytest.mark.asyncio
async def test_flag_off_runs_to_step_cap_and_never_queries(monkeypatch):
    monkeypatch.delenv("RUN_BUDGET_USD", raising=False)
    calls = []
    async def rollup(*a, **k):
        calls.append(1)
        return {"api_cost_usd": 99.0}
    monkeypatch.setattr(rb, "usage_rollup", rollup)
    agent = _build_agent()
    await agent.run(max_steps=3, _continue_session=True)
    assert agent.step.await_count == 3
    assert calls == []                           # zero overhead when disabled
    assert _result_session_status(agent.history) == "completed"


@pytest.mark.asyncio
async def test_sub_agent_is_ungated(monkeypatch):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    monkeypatch.setattr(rb, "usage_rollup", _rollup_sequence([9.9]))
    agent = _build_agent()
    agent._is_sub_agent = True
    await agent.run(max_steps=2, _continue_session=True)
    assert agent.step.await_count == 2
