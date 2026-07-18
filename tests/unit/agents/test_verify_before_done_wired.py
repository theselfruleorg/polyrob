"""I-3 / H3 (dedup decision D1) — verify-before-done nudge wired into the real
run-loop is_done choke point (agents/task/agent/core/run_loop.py:~455).

Mirrors tests/unit/agents/test_output_validation_wired.py's
``_build_run_loop_agent`` harness (a minimal ``Agent`` standing in for a real
``run()``, stubbing everything run() touches except the block under test) so
this test drives the ACTUAL run-loop code, not a reimplementation of it.

Design pinned here:
- flag OFF (default) -> byte-identical legacy: done() breaks immediately even
  when the ledger shows an unverified edit.
- flag ON + no unverified edit -> done() breaks immediately (no nudge).
- flag ON + unverified edit -> the first two done() calls are intercepted with
  a guidance nudge (kind="intervention") and `continue`, self-bounding at 2
  attempts; the third done() call is allowed through.
"""
from __future__ import annotations

import logging
import types

import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.task.agent.service import Agent
from agents.task.agent.views import ActionResult


def _build_run_loop_agent(*, max_steps=10):
    """Minimal Agent standing in for a real run(), stubbing everything run()
    touches except the done-handling verify-before-done block under test.
    Every step() call returns a fresh done() result (mirrors the output-
    validation harness) so the loop's only real decision is the block under
    test.
    """
    a = object.__new__(Agent)
    a.logger = logging.getLogger("verify-before-done-test")
    a._cancelled = False
    a.task = "edit some code"
    a.initial_actions = None
    a.generate_gif = False
    a.stall_timeout_seconds = None  # skip the stall-monitor task entirely
    a.validate_output = False  # isolate the verify-before-done block
    a._is_sub_agent = False
    a.agent_id = "agent_test-session"

    a.state = types.SimpleNamespace(
        n_steps=0, consecutive_failures=0, reset_llm_errors=MagicMock(),
    )

    done_result = [ActionResult(is_done=True, extracted_content="final answer", success=True)]

    async def _fake_step(step_info=None):
        a._last_result = done_result
        a.state.n_steps += 1

    a.step = AsyncMock(side_effect=_fake_step)
    a._last_result = []

    a._too_many_failures = MagicMock(return_value=False)
    a._handle_control_flags = AsyncMock(return_value=False)
    a._check_context_overflow = MagicMock(return_value=False)
    a._drain_user_messages = AsyncMock(return_value=[])
    a._maybe_spawn_background_review = MagicMock()

    a.telemetry_manager = MagicMock()
    a.telemetry_manager.flush_buffers = MagicMock()
    a.telemetry_manager.capture_session_end = MagicMock()
    a.telemetry_manager.capture_event = MagicMock()

    a.orchestrator = MagicMock()
    a.orchestrator.browser_manager = None
    a.orchestrator.user_id = None  # Agent.user_id is a read-only property -> orchestrator.user_id

    a.message_manager = MagicMock()
    a.message_manager.get_token_count = MagicMock(return_value=0)
    a.message_manager.model_name = "test-model"  # Agent.model_name reads message_manager.model_name

    a.task_context_manager = None
    a.usage_tracker = None
    a.register_done_callback = None

    a.history = MagicMock()
    a.history.history = []
    a.history.is_done = MagicMock(return_value=True)
    a.history.errors = MagicMock(return_value=[])

    return a, max_steps


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical_legacy(monkeypatch):
    """VERIFY_BEFORE_DONE unset (default OFF): done() breaks on the FIRST
    call even though the ledger would flag an unverified edit — the flag is
    the single gate, so off must not change behavior at all."""
    monkeypatch.delenv("VERIFY_BEFORE_DONE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    monkeypatch.setattr(
        "agents.task.runtime.edit_verify.edited_since_last_test",
        MagicMock(return_value=True),
    )

    agent, max_steps = _build_run_loop_agent(max_steps=10)

    await agent.run(max_steps=max_steps, _continue_session=True)

    assert agent.step.await_count == 1, "off must break on the very first done()"
    agent.message_manager.inject_user_guidance.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_no_unverified_edit_breaks_immediately(monkeypatch):
    monkeypatch.setenv("VERIFY_BEFORE_DONE", "true")
    monkeypatch.setattr(
        "agents.task.runtime.edit_verify.edited_since_last_test",
        MagicMock(return_value=False),
    )

    agent, max_steps = _build_run_loop_agent(max_steps=10)

    await agent.run(max_steps=max_steps, _continue_session=True)

    assert agent.step.await_count == 1, "no unverified edit -> no nudge, breaks immediately"
    agent.message_manager.inject_user_guidance.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_unverified_edit_nudges_twice_then_allows_done(monkeypatch):
    """Bounded to 2 attempts: done() calls 1 and 2 are intercepted (continue),
    done() call 3 is allowed through (breaks) -- the ledger-derived signal is
    never re-checked against the (stubbed, still-True) mock a third time in a
    way that would loop forever; the nudge counter itself is the bound."""
    monkeypatch.setenv("VERIFY_BEFORE_DONE", "true")
    monkeypatch.setattr(
        "agents.task.runtime.edit_verify.edited_since_last_test",
        MagicMock(return_value=True),
    )

    agent, max_steps = _build_run_loop_agent(max_steps=10)

    await agent.run(max_steps=max_steps, _continue_session=True)

    # 2 nudged attempts + 1 final allowed-through done() = 3 step() calls.
    assert agent.step.await_count == 3
    assert agent._verify_nudge_count == 2

    calls = agent.message_manager.inject_user_guidance.call_args_list
    assert len(calls) == 2
    first_batch = calls[0].args[0]
    assert first_batch[0]["kind"] == "intervention"
    assert first_batch[0]["metadata"]["source"] == "verify_before_done"
    assert first_batch[0]["metadata"]["attempt"] == 1
    assert calls[1].args[0][0]["metadata"]["attempt"] == 2


@pytest.mark.asyncio
async def test_flag_on_unverified_edit_bounded_by_max_steps(monkeypatch):
    """If max_steps is smaller than the nudge bound would need, the outer
    `for step_num in range(max_steps)` loop still terminates the run -- no
    unbounded retry regardless of the nudge cap."""
    monkeypatch.setenv("VERIFY_BEFORE_DONE", "true")
    monkeypatch.setattr(
        "agents.task.runtime.edit_verify.edited_since_last_test",
        MagicMock(return_value=True),
    )

    agent, max_steps = _build_run_loop_agent(max_steps=1)

    await agent.run(max_steps=max_steps, _continue_session=True)

    assert agent.step.await_count == 1
    assert agent._verify_nudge_count == 1
