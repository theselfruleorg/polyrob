"""An owner message that arrives DURING the step that calls done() must get a turn.

Prod (2026-09-18 08:12:33Z): the owner corrected a directive ("Ok I meant
0.04. From 0.04 to 0.1 eth") while Rob's session was mid-step. The run loop
drained the message after the step, injected it at the history tail — and
then saw the step's done() and broke. The HITL queue was now empty, so the
harness logged "completed with no pending input — skipping no-op resume".
The correction was never answered.

Rule: if messages were drained in the same loop iteration whose results carry
done(), the run continues (next step reads the injected guidance) instead of
breaking. Drives the REAL run loop via the verify-before-done harness.
"""
import pytest
from unittest.mock import AsyncMock

from agents.task.agent.views import ActionResult
from tests.unit.agents.test_verify_before_done_wired import _build_run_loop_agent


@pytest.mark.asyncio
async def test_message_drained_on_the_done_step_keeps_the_run_going(monkeypatch):
    monkeypatch.delenv("VERIFY_BEFORE_DONE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    agent, max_steps = _build_run_loop_agent(max_steps=10)
    owner_msg = {"text": "Ok I meant 0.04. From 0.04 to 0.1 eth", "kind": "user"}
    # run() drains once at start (empty), then after every step: the drain after the
    # first done() step returns the late message; later drains are empty.
    agent._drain_user_messages = AsyncMock(side_effect=[[], [owner_msg], [], [], []])

    await agent.run(max_steps=max_steps, _continue_session=True)

    assert agent.step.await_count == 2, "the late message must earn a second step, not be swallowed by done()"
    agent.message_manager.inject_user_guidance.assert_called_once()
    assert agent.message_manager.inject_user_guidance.call_args.args[0] == [owner_msg]


@pytest.mark.asyncio
async def test_no_late_message_done_still_breaks_immediately(monkeypatch):
    monkeypatch.delenv("VERIFY_BEFORE_DONE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    agent, max_steps = _build_run_loop_agent(max_steps=10)
    await agent.run(max_steps=max_steps, _continue_session=True)
    assert agent.step.await_count == 1
