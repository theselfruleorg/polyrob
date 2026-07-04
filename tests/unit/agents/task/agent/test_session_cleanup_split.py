"""P9 pass-22 — SessionCleanupMixin split out of orchestrator.py."""


def test_orchestrator_composes_cleanup_mixin():
    from agents.task.agent.orchestrator import SessionOrchestrator
    from agents.task.session.cleanup import SessionCleanupMixin
    assert issubclass(SessionOrchestrator, SessionCleanupMixin)
    assert SessionOrchestrator.cleanup.__qualname__.startswith("SessionCleanupMixin")


def test_cleanup_module_imports_cleanly():
    import agents.task.session.cleanup as c
    assert c.SessionCleanupMixin is not None
