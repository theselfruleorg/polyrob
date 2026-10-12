"""W9: the Agent's step/result and LLM-call state live in owned objects; the old
attribute names forward to them (agents/task/agent/core/run_state.py)."""
from agents.task.agent.core.run_state import LLMCallState, StepResultState, owned
from agents.task.agent.service import Agent
from agents.task.agent.views import ActionResult


def test_old_names_forward_to_the_owner_objects():
    a = object.__new__(Agent)
    assert a._last_result is None and a._llm_call_in_progress is False
    a._last_result = [ActionResult(is_done=True, extracted_content="ok")]
    assert isinstance(a.step_state, StepResultState)
    assert a.step_state.last_result is a._last_result and a.step_state.is_done()
    a.step_state.reset()
    assert a._last_result is None
    a._llm_call_in_progress, a._llm_call_start_time = True, 12.5
    assert isinstance(a.llm_call, LLMCallState)
    assert (a.llm_call.in_progress, a.llm_call.start_time) == (True, 12.5)
    a.llm_call.end()
    assert (a._llm_call_in_progress, a._llm_call_start_time) == (False, None)


def test_the_descriptors_live_on_agent_only():
    for name in ("_last_result", "_llm_call_in_progress", "_llm_call_start_time"):
        assert isinstance(Agent.__dict__[name], owned), name


def test_step_state_errors():
    s = StepResultState([ActionResult(error="boom"), ActionResult(extracted_content="x")])
    assert s.errors() == ["boom"] and not s.is_done()
