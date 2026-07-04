"""P9 pass-20 — ResultProcessingMixin split out of step.py."""


def test_agent_composes_result_processing_mixin():
    from agents.task.agent.service import Agent
    from agents.task.agent.core.result_processing import ResultProcessingMixin
    assert issubclass(Agent, ResultProcessingMixin)
    for m in ("_add_tool_messages", "_process_action_results"):
        assert getattr(Agent, m).__qualname__.startswith("ResultProcessingMixin")


def test_result_processing_imports_cleanly():
    import agents.task.agent.core.result_processing as rp
    assert rp.ResultProcessingMixin is not None
