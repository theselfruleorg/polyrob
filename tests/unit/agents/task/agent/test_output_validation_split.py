"""P9 pass-17 — OutputValidationMixin split out of llm_runner.py."""


def test_agent_composes_output_validation_mixin():
    from agents.task.agent.service import Agent
    from agents.task.agent.core.output_validation import OutputValidationMixin
    assert issubclass(Agent, OutputValidationMixin)
    for m in ("_get_llm_parameters", "_validate_output", "next_action"):
        assert getattr(Agent, m).__qualname__.startswith("OutputValidationMixin")


def test_llm_runner_still_owns_invocation():
    from agents.task.agent.core.llm_runner import LLMRunnerMixin
    for m in ("get_next_action", "_validate_model_output"):
        assert hasattr(LLMRunnerMixin, m)
