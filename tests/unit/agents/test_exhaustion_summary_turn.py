"""083 WS-2: the last report at the step limit.

At the cap the agent gets exactly ONE extra model call (same tools, request on
the ephemeral rail). Its reply is recorded as the agent's words; nothing in it
runs — not even done — so an exhausted run is never a board success (T2-01).
Scope: autonomous runs and sub-agents; chat skips the call. Fail-open.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.task.agent.views import ActionResult
from agents.task.runtime.run_as_session import completed_via_done
from agents.task.runtime.stop_reason import LAST_REPORT_LABEL
from tests.unit.agents.test_verify_before_done_wired import _build_run_loop_agent

REPORT = "Finished: wrote report.md (sha 1a2b). Remains: post it. Blocked: nothing."


class _Act:
    def __init__(self, name, params):
        self._d = {name: params}

    def model_dump(self, exclude_unset=True):
        return dict(self._d)


def _model_output(*actions, memory="brain memory"):
    return SimpleNamespace(action=list(actions),
                           current_state=SimpleNamespace(memory=memory))


def _work():
    return [ActionResult(extracted_content="did a thing", include_in_memory=True)]


def _agent(*, sub=True, max_steps=2, reply=None):
    agent, _ = _build_run_loop_agent(max_steps=max_steps)
    agent.max_failures = 3
    agent._is_sub_agent = sub
    agent.tool_call_tracker = None

    async def _fake_step(step_info=None):
        agent._last_result = _work()
        agent.state.n_steps += 1
    agent.step = AsyncMock(side_effect=_fake_step)
    agent.get_next_action = AsyncMock(return_value=reply if reply is not None else _model_output(
        _Act("done", {"text": REPORT})))
    agent.controller.multi_act = AsyncMock()
    agent.message_manager.get_messages = MagicMock(return_value=["msgs"])
    return agent, max_steps


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("VERIFY_BEFORE_DONE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("RUN_BUDGET_USD", raising=False)
    events = []
    monkeypatch.setattr("core.event_log.emit",
                        lambda kind, **kw: events.append((kind, kw.get("attrs"))))
    return events


async def _run(agent, n):
    await agent.run(max_steps=n, _continue_session=True)


@pytest.mark.asyncio
async def test_one_extra_call_records_the_report_and_runs_nothing(_env):
    agent, n = _agent()
    await _run(agent, n)
    assert agent.step.await_count == n
    agent.get_next_action.assert_awaited_once_with(["msgs"])
    agent.controller.multi_act.assert_not_called()          # no action ran, done included
    agent.message_manager.push_ephemeral_message.assert_called_once()
    agent.message_manager.commit_ephemeral_consumption.assert_called()
    assert agent.state.stop_reason == "max_steps"
    assert agent.state.exhausted_summary == REPORT
    item = agent._last_result[0]
    assert item.is_done is False and item.metadata == {"exhausted_summary": True}
    assert item.extracted_content == REPORT
    assert agent.history.history[-1].result == agent._last_result
    agent.message_manager.add_plan.assert_called_once_with(LAST_REPORT_LABEL + REPORT)
    # T2-01: the run still did not finish.
    orch = SimpleNamespace(agents={"m": SimpleNamespace(_is_sub_agent=False,
                                                        _last_result=agent._last_result)})
    assert completed_via_done(orch) is False
    assert ("exhaustion_summary", {"chars": len(REPORT), "max_steps": n}) in _env


@pytest.mark.asyncio
async def test_send_message_text_then_memory_are_fallbacks():
    agent, n = _agent(reply=_model_output(_Act("read_file", {"path": "x"}),
                                          _Act("send_message", {"text": "sent words"})))
    await _run(agent, n)
    assert agent.state.exhausted_summary == "sent words"

    agent, n = _agent(reply=_model_output(_Act("read_file", {"path": "x"}), memory="mem words"))
    await _run(agent, n)
    assert agent.state.exhausted_summary == "mem words"


@pytest.mark.asyncio
async def test_llm_error_is_todays_behaviour(_env):
    agent, n = _agent()
    agent.get_next_action = AsyncMock(side_effect=RuntimeError("provider down"))
    await _run(agent, n)
    assert agent.state.stop_reason == "max_steps"
    assert agent.state.exhausted_summary is None
    assert agent._last_result == _work()                     # no summary item
    assert agent.history.history == []
    agent.message_manager.commit_ephemeral_consumption.assert_called()
    assert any(k == "exhaustion_summary_failed" for k, _ in _env)


@pytest.mark.asyncio
async def test_budget_halt_means_no_call(monkeypatch):
    monkeypatch.setattr("agents.task.agent.core.run_budget.check_run_budget",
                        AsyncMock(return_value="run_budget_exhausted: x"))
    agent, n = _agent()
    await _run(agent, n)
    agent.get_next_action.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_means_no_call():
    agent, n = _agent()

    async def _step(step_info=None):
        agent._last_result = _work()
        agent.state.n_steps += 1
        agent._cancelled = agent.state.n_steps >= n
    agent.step = AsyncMock(side_effect=_step)
    await _run(agent, n)
    agent.get_next_action.assert_not_called()


@pytest.mark.asyncio
async def test_stopped_state_means_no_call():
    agent, n = _agent()
    agent.state.stopped = True
    await _run(agent, n)
    agent.get_next_action.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_error_propagates():
    agent, n = _agent()
    agent.get_next_action = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _run(agent, n)


@pytest.mark.asyncio
async def test_chat_run_skips_the_call_but_keeps_the_stop_reason():
    agent, n = _agent(sub=False)
    await _run(agent, n)
    agent.get_next_action.assert_not_called()
    assert agent.state.stop_reason == "max_steps"
    assert agent.state.exhausted_summary is None


@pytest.mark.asyncio
async def test_autonomous_main_run_gets_the_call(monkeypatch):
    monkeypatch.setattr("agents.task.session_class.budget_class_autonomous",
                        lambda sid: sid == "sess-auto")
    agent, n = _agent(sub=False)
    agent.orchestrator.session_id = "sess-auto"
    await _run(agent, n)
    agent.get_next_action.assert_awaited_once()
    assert agent.state.exhausted_summary == REPORT


@pytest.mark.asyncio
async def test_done_run_never_calls():
    agent, n = _agent()

    async def _step(step_info=None):
        agent._last_result = [ActionResult(is_done=True, extracted_content="x")]
        agent.state.n_steps += 1
    agent.step = AsyncMock(side_effect=_step)
    await _run(agent, n)
    agent.get_next_action.assert_not_called()
    assert agent.state.stop_reason == "done"


def test_run_outcome_carries_the_report_and_stays_not_done():
    from agents.task.runtime.run_outcome import build_run_outcome
    item = [ActionResult(extracted_content=REPORT, is_done=False,
                         metadata={"exhausted_summary": True})]
    main = SimpleNamespace(_is_sub_agent=False, _last_result=item,
                           history=SimpleNamespace(history=[SimpleNamespace(
                               model_output=None, result=item)]),
                           state=SimpleNamespace(stop_reason="max_steps", max_steps=30,
                                                 exhausted_summary=REPORT, n_steps=30))

    class _TA:
        def get_orchestrator(self, sid):
            return SimpleNamespace(agents={"m": main}, session_id=sid, usage_tracker=None)

    o = asyncio.run(build_run_outcome(_TA(), "s1", "Session completed successfully"))
    assert o.done_called is False and o.done_text == ""
    assert o.exhausted_summary == REPORT and o.stop_reason == "max_steps"
