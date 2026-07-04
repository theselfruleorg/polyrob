"""P9 pass-24 — NextActionInternalMixin split out of llm_runner.py (-> llm_runner <700)."""


def test_agent_composes_next_action_internal_mixin():
    from agents.task.agent.service import Agent
    from agents.task.agent.core.next_action_internal import NextActionInternalMixin
    assert issubclass(Agent, NextActionInternalMixin)
    assert Agent._get_next_action_internal.__qualname__.startswith("NextActionInternalMixin")


def test_llm_runner_keeps_get_next_action():
    from agents.task.agent.core.llm_runner import LLMRunnerMixin
    assert hasattr(LLMRunnerMixin, "get_next_action")
    assert hasattr(LLMRunnerMixin, "_validate_model_output")
