"""P9 pass-21 — StepExecutionMixin split out of step.py (-> step.py <700)."""


def test_agent_composes_step_execution_mixin():
    from agents.task.agent.service import Agent
    from agents.task.agent.core.step_execution import StepExecutionMixin
    assert issubclass(Agent, StepExecutionMixin)
    for m in ("_validate_and_intervene", "_build_execution_context", "_execute_actions"):
        assert getattr(Agent, m).__qualname__.startswith("StepExecutionMixin")


def test_step_execution_imports_cleanly():
    import agents.task.agent.core.step_execution as se
    assert se.StepExecutionMixin is not None
