"""P9 pass-15 — HITLIngressMixin split out of orchestrator.py."""


def test_orchestrator_composes_hitl_ingress_mixin():
    from agents.task.agent.orchestrator import SessionOrchestrator
    from agents.task.session.hitl_ingress import HITLIngressMixin
    assert issubclass(SessionOrchestrator, HITLIngressMixin)
    for m in ("submit_user_message", "record_decision", "get_pending_messages", "get_recent_messages"):
        assert getattr(SessionOrchestrator, m).__qualname__.startswith("HITLIngressMixin")


def test_hitl_ingress_imports_cleanly():
    import agents.task.session.hitl_ingress as h
    assert h.HITLIngressMixin is not None
