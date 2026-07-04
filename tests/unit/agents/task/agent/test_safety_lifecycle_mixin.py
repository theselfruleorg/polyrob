"""P9 pass-4 — SafetyLifecycleMixin extracted from service.py."""
import types

from agents.task.agent.core.safety_lifecycle import SafetyLifecycleMixin


def test_agent_composes_safety_lifecycle_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, SafetyLifecycleMixin)
    for m in ("_too_many_failures", "_check_context_overflow", "_check_for_stall",
              "_stall_monitor_loop", "_handle_control_flags", "pause", "resume",
              "stop", "cancel", "reset_for_continuation"):
        assert getattr(Agent, m).__qualname__.startswith("SafetyLifecycleMixin")


class _Host(SafetyLifecycleMixin):
    def __init__(self):
        import logging
        self.logger = logging.getLogger("safety-test")
        self.max_failures = 3
        self.state = types.SimpleNamespace(consecutive_failures=0, stopped=False)


def test_too_many_failures_threshold():
    h = _Host()
    assert h._too_many_failures() is False
    h.state.consecutive_failures = 3
    assert h._too_many_failures() is True


def test_pause_redirects_to_stopped():
    h = _Host()
    h.pause()
    assert h.state.stopped is True


def test_cancel_sets_flag():
    h = _Host()
    h.cancel()
    assert h._cancelled is True
