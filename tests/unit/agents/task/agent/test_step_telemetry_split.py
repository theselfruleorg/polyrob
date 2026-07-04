"""P9 pass-16 — StepTelemetryMixin split out of step.py."""


def test_agent_composes_step_telemetry_mixin():
    from agents.task.agent.service import Agent
    from agents.task.agent.core.step_telemetry import StepTelemetryMixin
    assert issubclass(Agent, StepTelemetryMixin)
    assert Agent._emit_step_telemetry.__qualname__.startswith("StepTelemetryMixin")


def test_step_telemetry_imports_cleanly():
    import agents.task.agent.core.step_telemetry as st
    assert st.StepTelemetryMixin is not None
