"""083 WS-1: every run-loop exit names its typed stop reason.

Before this, every exit without done() was recorded as "likely ran out of
steps". Drives the REAL run loop through the verify-before-done harness: one
case per exit, plus the first-writer-wins rule and the plain-words text.
"""
import asyncio
import itertools
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.task.agent.views import ActionResult
from agents.task.runtime.stop_reason import (
    StopReason, describe_stop_reason, done_stop_reason, set_stop)
from tests.unit.agents.test_verify_before_done_wired import _build_run_loop_agent

INFRA = ("broadcast failed: [Errno 13] Permission denied: "
         "'/var/lib/polyrob/wallet/submissions.sqlite' — nothing was sent")


def _agent(results, *, max_steps=5):
    agent, _ = _build_run_loop_agent(max_steps=max_steps)
    agent.state.stop_reason = "stale"  # a previous run's reason must be reset
    agent.max_failures = 3
    seq = iter(results)

    async def _fake_step(step_info=None):
        agent._last_result = next(seq)
        agent.state.n_steps += 1
    agent.step = AsyncMock(side_effect=_fake_step)
    return agent, max_steps


def _work():
    return [ActionResult(extracted_content="did a thing", include_in_memory=True)]


def _reply():
    return [ActionResult(extracted_content="hi", metadata={"conversational_reply": True})]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("VERIFY_BEFORE_DONE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("RUN_BUDGET_USD", raising=False)


async def _run(agent, max_steps):
    await agent.run(max_steps=max_steps, _continue_session=True)
    return agent.state.stop_reason


@pytest.mark.asyncio
async def test_done():
    agent, n = _agent([[ActionResult(is_done=True, extracted_content="x", success=True)]])
    assert await _run(agent, n) == "done"


@pytest.mark.asyncio
async def test_awaiting_reply():
    paused = [ActionResult(extracted_content="Message sent to user. Task paused - will resume "
                           "when user responds.", is_done=True,
                           metadata={"awaiting_reply": True})]
    agent, n = _agent([paused])
    assert await _run(agent, n) == "awaiting_reply"


@pytest.mark.asyncio
async def test_fatal_done_with_error_is_error():
    agent, n = _agent([[ActionResult(is_done=True, error="PERMANENT ERROR: 402")]])
    assert await _run(agent, n) == "error"


@pytest.mark.asyncio
async def test_max_steps():
    agent, n = _agent([_work() for _ in range(3)], max_steps=3)
    assert await _run(agent, n) == "max_steps"
    assert agent.step.await_count == 3


@pytest.mark.asyncio
async def test_a_last_step_stopped_by_the_user_is_control_not_max_steps():
    agent, n = _agent([_work() for _ in range(3)], max_steps=3)
    inner = agent.step.side_effect

    async def _stop_on_last(step_info=None):
        await inner(step_info)
        if agent.state.n_steps == 3:
            agent.state.stopped = True
    agent.step.side_effect = _stop_on_last
    assert await _run(agent, n) == "control"


@pytest.mark.asyncio
async def test_failures():
    agent, n = _agent([_work()])
    agent._too_many_failures = MagicMock(return_value=True)
    assert await _run(agent, n) == "failures"


@pytest.mark.asyncio
async def test_run_budget(monkeypatch):
    monkeypatch.setattr("agents.task.agent.core.run_loop.check_run_budget",
                        AsyncMock(return_value="run_budget_exhausted: $1 >= $1"))
    agent, n = _agent([_work()])
    assert await _run(agent, n) == "run_budget"
    assert agent.step.await_count == 0


@pytest.mark.asyncio
async def test_control_flag_break():
    agent, n = _agent([_work()])
    agent._handle_control_flags = AsyncMock(return_value=True)
    assert await _run(agent, n) == "control"


@pytest.mark.asyncio
async def test_control_flag_interrupted_reraises():
    agent, n = _agent([_work()])
    agent._handle_control_flags = AsyncMock(side_effect=InterruptedError("Agent stopped"))
    with pytest.raises(InterruptedError):
        await agent.run(max_steps=n, _continue_session=True)
    assert agent.state.stop_reason == "control"


@pytest.mark.asyncio
async def test_cooperative_cancel():
    agent, n = _agent([_work()])
    agent._cancelled = True
    assert await _run(agent, n) == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_error_reraises():
    agent, n = _agent([])
    agent.step = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await agent.run(max_steps=n, _continue_session=True)
    assert agent.state.stop_reason == "cancelled"


@pytest.mark.asyncio
async def test_money_infra():
    agent, n = _agent([[ActionResult(error=INFRA, include_in_memory=True)]])
    assert await _run(agent, n) == "money_infra"


@pytest.mark.asyncio
async def test_conversational_exit():
    agent, n = _agent([_reply() for _ in range(5)])
    assert await _run(agent, n) == "conversational_exit"


@pytest.mark.asyncio
async def test_stall(monkeypatch):
    import agents.task.agent.core.run_loop as rl
    clock = itertools.count(0, 1000)
    monkeypatch.setattr(rl.time, "time", lambda: float(next(clock)))
    agent, n = _agent([_work()])
    assert await _run(agent, n) == "stall"
    assert agent.step.await_count == 0


@pytest.mark.asyncio
async def test_fatal_step_error_wins_over_failures():
    """step.py's fatal branch writes `error` first; the next iteration's
    too-many-failures exit must not overwrite it."""
    agent, n = _agent([])

    async def _fatal(step_info=None):
        set_stop(agent, StopReason.ERROR)
        agent._last_result = [ActionResult(error="FATAL ERROR: boom")]
        agent._too_many_failures = MagicMock(return_value=True)
    agent.step = AsyncMock(side_effect=_fatal)
    assert await _run(agent, n) == "error"


@pytest.mark.asyncio
async def test_telemetry_carries_stop_reason():
    agent, n = _agent([_work() for _ in range(2)], max_steps=2)
    await _run(agent, n)
    event = agent.telemetry_manager.capture_event.call_args[0][0]
    assert event.stop_reason == "max_steps"


def test_describe_plain_words():
    assert describe_stop_reason("max_steps", 30, 30) == "stopped at the step limit (30/30)"
    assert describe_stop_reason(StopReason.FAILURES, 4, 30) == (
        "stopped after too many consecutive failed steps (4/30)")
    assert describe_stop_reason("wall_clock", 7, None) == "cut by the wall-clock cap (step 7)"
    assert describe_stop_reason(None) == "stop reason unknown"
    assert describe_stop_reason("bogus", 1, 2) == "stop reason 'bogus' (1/2)"


def test_set_stop_first_writer_wins_and_never_raises():
    agent = MagicMock()
    agent.state.stop_reason = None
    set_stop(agent, StopReason.STALL)
    set_stop(agent, StopReason.FAILURES)
    assert agent.state.stop_reason == "stall"
    set_stop(object(), StopReason.DONE)  # no state: no raise


def test_done_stop_reason():
    assert done_stop_reason([ActionResult(is_done=True)]) is StopReason.DONE
    assert done_stop_reason(None) is StopReason.DONE
