"""P9 pass-4 — SafetyLifecycleMixin extracted from service.py."""
import types

from agents.task.agent.core.safety_lifecycle import SafetyLifecycleMixin


def test_agent_composes_safety_lifecycle_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, SafetyLifecycleMixin)
    for m in ("_too_many_failures", "_check_for_stall",
              "_stall_monitor_loop", "_handle_control_flags", "pause", "resume",
              "stop", "cancel", "reset_for_continuation"):
        assert getattr(Agent, m).__qualname__.startswith("SafetyLifecycleMixin")


class _Host(SafetyLifecycleMixin):
    def __init__(self):
        import logging
        self.logger = logging.getLogger("safety-test")
        self.max_failures = 3
        from agents.task.agent.agent_state import AgentState
        self.state = AgentState()


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


def test_reset_for_continuation_clears_acted_this_run():
    """Release review: ``_acted_this_run`` is per RUN (call_quality E-2: a prose
    reply is a final report only once THIS run acted). A reused agent kept it
    True, so the new run's opening plan could end the run as its report.
    098 AG-02: the counter lives in RunControl; begin_run clears it."""
    from agents.task.agent.core.run_state import run_field

    class _AgentLike(_Host):
        _acted_this_run = run_field("acted_this_run")

    h = _AgentLike()
    h.message_manager = types.SimpleNamespace(history=types.SimpleNamespace(messages=[]))
    h._acted_this_run = True
    h.reset_for_continuation()
    assert h._acted_this_run is False
