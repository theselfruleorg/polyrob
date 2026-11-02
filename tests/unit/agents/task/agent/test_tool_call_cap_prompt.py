"""2026-09-19 (the agent's self-review): "a 6-call batch silently dropped the 6th call".
The system prompt said "Call 1-10 functions" (config `max_actions_per_step`) while the
executor hard-caps a step at `MAX_TOOL_CALLS_PER_STEP` (5) and defers the rest with an
ephemeral notice the model can miss. The prompt must state the EFFECTIVE cap — one
constant, imported, not two numbers that drift — and the deferral notice must say the
deferred calls did NOT run."""
from agents.task.agent.core.step_execution import MAX_TOOL_CALLS_PER_STEP
from agents.task.agent.prompts import SystemPrompt


def test_prompt_states_the_executor_cap_not_the_config_ceiling():
    p = SystemPrompt(action_description="x", use_native_tools=True, model_name="gpt-4",
                     provider="openai", max_actions_per_step=10)
    text = p.get_system_message().content
    assert f"Call 1-{MAX_TOOL_CALLS_PER_STEP} functions" in text
    assert "Call 1-10 functions" not in text
    assert f"more than {MAX_TOOL_CALLS_PER_STEP}" in text  # the consequence is spelled out


def test_prompt_respects_a_lower_config_cap():
    p = SystemPrompt(action_description="x", use_native_tools=True, model_name="gpt-4",
                     provider="openai", max_actions_per_step=3)
    assert "Call 1-3 functions" in p.get_system_message().content


def test_cap_constant_is_module_level():
    assert isinstance(MAX_TOOL_CALLS_PER_STEP, int) and MAX_TOOL_CALLS_PER_STEP >= 1


def _cap_agent(per_agent):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    return SimpleNamespace(max_actions_per_step=per_agent, logger=MagicMock(),
                           message_manager=MagicMock())


def _named(n):
    from types import SimpleNamespace
    return [SimpleNamespace(name=f"call{i}") for i in range(n)]


def test_a_reduced_cap_names_the_dropped_calls():
    """095 W1d (HI-8): after parse errors lower max_actions_per_step, the calls past
    the reduced cap get the named "did NOT run" note — never a silent drop."""
    from agents.task.agent.core.step_execution import apply_step_cap
    agent = _cap_agent(2)
    run = apply_step_cap(agent, _named(4))
    assert [a.name for a in run] == ["call0", "call1"]
    note = agent.message_manager.push_ephemeral_message.call_args[0][0].content
    assert "2 call(s) did NOT run" in note
    assert "call2, call3" in note
    assert "at most 2 tool calls (this agent's cap" in note


def test_the_executor_cap_still_binds_a_high_config():
    from agents.task.agent.core.step_execution import apply_step_cap
    agent = _cap_agent(10)
    run = apply_step_cap(agent, _named(MAX_TOOL_CALLS_PER_STEP + 1))
    assert len(run) == MAX_TOOL_CALLS_PER_STEP
    note = agent.message_manager.push_ephemeral_message.call_args[0][0].content
    assert "this agent's cap" not in note
    agent = _cap_agent(10)
    assert len(apply_step_cap(agent, _named(3))) == 3
    agent.message_manager.push_ephemeral_message.assert_not_called()
