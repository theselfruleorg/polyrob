"""P9 pass-25 — RunLoopMixin split out of service.py."""


def test_agent_composes_run_loop_mixin():
    from agents.task.agent.service import Agent
    from agents.task.agent.core.run_loop import RunLoopMixin
    assert issubclass(Agent, RunLoopMixin)
    assert Agent.run.__qualname__.startswith("RunLoopMixin")


def test_run_loop_imports_cleanly():
    import agents.task.agent.core.run_loop as r
    assert r.RunLoopMixin is not None
