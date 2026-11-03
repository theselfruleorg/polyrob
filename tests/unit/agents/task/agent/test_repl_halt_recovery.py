"""098 AG-01: one halt must not wedge the REPL; failed providers are run-scoped.

The run-scoped halt is cleared by ``AgentState.begin_run`` (``run()`` calls it
first), so the REPL reset no longer needs to know the flags."""
from types import SimpleNamespace

from agents.task.agent.agent_state import AgentState
from agents.task.agent.conversation import _reset_turn_state
from agents.task.runtime.stop_reason import StopReason


def _agent():
    state = AgentState()
    return SimpleNamespace(state=state, _cancelled=True,
                           controller=SimpleNamespace(reset_retry_wall=lambda: None))


def test_begin_run_clears_stopped_after_halt():
    agent = _agent()
    agent.state.request_stop(StopReason.ERROR)
    agent.state.consecutive_failures = 3
    agent.state.begin_run()
    assert agent.state.stopped is False
    assert agent.state.stop_reason is None
    assert agent.state.consecutive_failures == 0


def test_begin_run_clears_failed_providers():
    agent = _agent()
    agent.state.llm_providers_failed.extend(["openai", "anthropic", "gemini"])
    agent.state.llm_circuit_breaker_open = True
    agent.state.begin_run()
    assert agent.state.llm_providers_failed == []
    assert agent.state.llm_circuit_breaker_open is False


def test_owner_line_reset_clears_cancel_flag():
    agent = _agent()
    _reset_turn_state(agent, owner_line=True)
    assert agent._cancelled is False
