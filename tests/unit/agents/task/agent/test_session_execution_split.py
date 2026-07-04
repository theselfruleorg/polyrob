"""P9 pass-23 — SessionExecutionMixin split out of orchestrator.py (-> orchestrator <700)."""


def test_orchestrator_composes_execution_mixin():
    from agents.task.agent.orchestrator import SessionOrchestrator
    from agents.task.session.execution import SessionExecutionMixin
    assert issubclass(SessionOrchestrator, SessionExecutionMixin)
    for m in ("create_agent", "execute_session"):
        assert getattr(SessionOrchestrator, m).__qualname__.startswith("SessionExecutionMixin")


def test_execution_module_imports_cleanly():
    import agents.task.session.execution as e
    assert e.SessionExecutionMixin is not None and e.Agent is not None
