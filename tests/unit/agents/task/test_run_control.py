"""098 AG-01/02/03: RunControl owns the run lifecycle (idle/running/paused/stopped)."""
import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.task.agent.agent_state import AgentState
from agents.task.agent.core.run_state import RUN_MACHINE, RunControl
from agents.task.agent.views import ActionResult
from agents.task.runtime.stop_reason import StopReason
from core.state import machine as sm


def _run_agent(max_steps=2):
    from tests.unit.agents.test_verify_before_done_wired import _build_run_loop_agent
    agent, _ = _build_run_loop_agent(max_steps=max_steps)
    agent.max_failures = 3

    async def _step(step_info=None):
        agent._last_result = [ActionResult(is_done=True, extracted_content="ok", success=True)]
        agent.state.n_steps += 1
    agent.step = AsyncMock(side_effect=_step)
    return agent


@pytest.mark.asyncio
async def test_run_after_stop_starts_clean(monkeypatch):
    monkeypatch.delenv("VERIFY_BEFORE_DONE", raising=False)
    agent = _run_agent()
    agent.state.request_stop(StopReason.ERROR)
    agent.state.consecutive_failures = agent.max_failures
    await agent.run(max_steps=2, _continue_session=True)
    assert agent.step.await_count == 1
    assert agent.state.stop_reason == "done"
    assert agent.state.run_control.state == "idle"


def test_failed_providers_cleared_per_run():
    st = AgentState()
    st.track_llm_error("LLMError", "openai")
    st.llm_circuit_breaker_open = True
    st.begin_run()
    assert st.llm_providers_failed == [] and not st.llm_circuit_breaker_open
    assert st.llm_error_type_counts == {}


def test_begin_run_clears_empty_action_counter():
    from agents.task.agent.service import Agent
    a = object.__new__(Agent)
    a.state = AgentState()
    a._empty_action_counter = 1
    a._acted_this_run = True
    a.state.begin_run()
    assert a._empty_action_counter == 0 and a._acted_this_run is False


def test_request_stop_is_first_reason_wins():
    rc = RunControl()
    rc.begin_run()
    assert rc.request_stop(StopReason.STALL) is True
    assert rc.request_stop(StopReason.FAILURES) is False
    assert rc.stopped and rc.stop_reason == "stall"


@pytest.mark.asyncio
async def test_stall_monitor_records_stall_reason():
    from agents.task.agent.core.safety_lifecycle import SafetyLifecycleMixin

    class _H(SafetyLifecycleMixin):
        pass
    h = _H()
    h.logger = logging.getLogger("t")
    h.state = AgentState()
    h.state.begin_run()
    h.max_failures = 3
    h._stall_check_interval = 0
    h._check_for_stall = AsyncMock(return_value=True)
    await h._stall_monitor_loop()
    assert h.state.stopped and h.state.stop_reason == "stall"


def test_illegal_transition_emits_event(monkeypatch):
    seen = []
    monkeypatch.setattr(sm, "_emit_illegal", lambda *a: seen.append(a))
    rc = RunControl()
    monkeypatch.setattr(sm, "_STRICT", False)
    rc.pause()                      # idle -> paused is not an edge
    assert rc.state == "idle"
    assert seen and seen[0][:3] == ("agent_run", "idle", "paused")


def test_run_machine_well_formed():
    assert RUN_MACHINE.validate() == []


# -- persistence ---------------------------------------------------------------

def test_run_flags_not_persisted(tmp_path):
    st = AgentState()
    st.request_stop(StopReason.ERROR)
    st.track_llm_error("LLMError", "openai")
    data = st.to_dict()
    for key in ("stopped", "paused", "done", "llm_providers_failed",
                "llm_circuit_breaker_open", "llm_error_type_counts"):
        assert key not in data
    assert data["version"] == "1.2"


def test_legacy_file_with_stopped_true_restores_runnable(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({"version": "1.1", "n_steps": 7, "stopped": True,
                                "done": True, "paused": True,
                                "llm_providers_failed": ["openai", "gemini", "anthropic"],
                                "llm_circuit_breaker_open": True}))
    st = AgentState.load_from_file(path)
    assert st is not None and st.n_steps == 7
    assert not st.stopped and st.llm_providers_failed == []
    assert not st.should_halt_for_llm_error()


# -- AG-03: pause is not a stall -------------------------------------------------

class _Store:
    def __init__(self, rows):
        self.rows = list(rows)
        self.acks = []

    def read(self):
        return self.rows.pop(0) if len(self.rows) > 1 else self.rows[0]

    def acknowledge(self, gen, cmd, ack):
        self.acks.append(ack)


@pytest.mark.asyncio
async def test_long_pause_does_not_stall(monkeypatch):
    from agents.task.agent.core import session_control as sc
    clock = {"t": 1000.0}
    monkeypatch.setattr(sc.time, "time", lambda: clock["t"])

    async def _sleep(_):
        clock["t"] += 900.0
    monkeypatch.setattr(sc.asyncio, "sleep", _sleep)
    agent = SimpleNamespace(state=AgentState())
    agent.state.begin_run()
    agent.state.last_activity_time = 0.0
    store = _Store([{"generation": 1, "request": "pause", "acknowledged": "running"},
                    {"generation": 1, "request": "resume", "acknowledged": "paused"}])
    agent._session_control = (store, 1)
    assert await sc.control_checkpoint(agent) is True
    rc = agent.state.run_control
    assert rc.state == "running" and rc.paused_total == pytest.approx(900.0)
    assert rc.resumed_at == clock["t"] and agent.state.last_activity_time == clock["t"]
    assert agent.state.stop_reason is None


@pytest.mark.asyncio
async def test_stall_monitor_skips_paused_run():
    from agents.task.agent.core.safety_lifecycle import SafetyLifecycleMixin

    class _H(SafetyLifecycleMixin):
        pass
    h = _H()
    h.logger = logging.getLogger("t")
    h.state = AgentState()
    h.state.begin_run()
    h.state.run_control.pause()
    h.state.last_activity_time = 0.0
    h.stall_timeout_seconds = 1
    assert await h._check_for_stall() is False


def test_pause_resume_transitions_are_legal():
    assert RUN_MACHINE.check("running", "paused") and RUN_MACHINE.check("paused", "running")
    rc = RunControl()
    rc.begin_run()
    rc.pause(now=10.0)
    assert rc.resume(now=25.0) == 15.0 and rc.state == "running"


# -- AG-19: LLMCallState is scoped -------------------------------------------------

def test_llm_call_state_cleared_on_exception():
    from agents.task.agent.core.run_state import LLMCallState
    st = LLMCallState()
    with pytest.raises(TypeError):
        with st.active(5.0):
            assert st.in_progress and st.start_time == 5.0
            raise TypeError("bad schema")
    assert st.in_progress is False and st.start_time is None


@pytest.mark.asyncio
async def test_llm_call_state_cleared_on_cancel():
    from agents.task.agent.core.run_state import LLMCallState
    st = LLMCallState()

    async def _call():
        with st.active(1.0):
            await asyncio.sleep(10)
    task = asyncio.create_task(_call())
    await asyncio.sleep(0)
    assert st.in_progress
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert st.in_progress is False


def test_llm_call_state_helper_shares_the_descriptor_owner():
    from agents.task.agent.core.run_state import llm_call_state
    from agents.task.agent.service import Agent
    a = object.__new__(Agent)
    with llm_call_state(a).active(3.0):
        assert a._llm_call_in_progress is True and a._llm_call_start_time == 3.0
    assert a._llm_call_in_progress is False
