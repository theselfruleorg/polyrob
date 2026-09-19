"""A money verb that fails for a HOST reason ends the run, verbatim, at once.

Intel MEDIUM (2026-09-17 19:35Z): on 09-16/17 every broadcast failed with
`[Errno 13] Permission denied … — nothing was sent`; the EXIT rails then burned
their step budgets on host forensics and the owner learned of it 5 h late with
the wrong cause. Drives the REAL run loop via the verify-before-done harness.
"""
import pytest
from unittest.mock import AsyncMock

from agents.task.agent.views import ActionResult
from tests.unit.agents.test_verify_before_done_wired import _build_run_loop_agent

INFRA = ("broadcast failed: [Errno 13] Permission denied: "
         "'/var/lib/polyrob/wallet/submissions.sqlite' — nothing was sent")


def _agent_with_results(seq):
    agent, max_steps = _build_run_loop_agent(max_steps=10)
    it = iter(seq)

    async def _fake_step(step_info=None):
        agent._last_result = next(it)
        agent.state.n_steps += 1
    agent.step = AsyncMock(side_effect=_fake_step)
    return agent, max_steps


@pytest.mark.asyncio
async def test_infra_broadcast_failure_stops_after_that_step(monkeypatch):
    monkeypatch.delenv("VERIFY_BEFORE_DONE", raising=False)
    infra = [ActionResult(error=INFRA, include_in_memory=True)]
    done = [ActionResult(is_done=True, extracted_content="x", success=True)]
    agent, max_steps = _agent_with_results([infra, done, done])
    await agent.run(max_steps=max_steps, _continue_session=True)
    assert agent.step.await_count == 1, "no forensics steps after a host-level broadcast failure"
    assert agent.state.stopped is True


@pytest.mark.asyncio
async def test_ordinary_tool_error_does_not_stop(monkeypatch):
    monkeypatch.delenv("VERIFY_BEFORE_DONE", raising=False)
    err = [ActionResult(error="tx_guard refused: per-tx cap $300 exceeded", include_in_memory=True)]
    done = [ActionResult(is_done=True, extracted_content="x", success=True)]
    agent, max_steps = _agent_with_results([err, done, done])
    await agent.run(max_steps=max_steps, _continue_session=True)
    assert agent.step.await_count == 2
    assert not getattr(agent.state, "stopped", False)
