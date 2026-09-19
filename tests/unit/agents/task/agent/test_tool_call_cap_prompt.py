"""2026-09-19 (Rob's self-review): "a 6-call batch silently dropped the 6th call".
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
