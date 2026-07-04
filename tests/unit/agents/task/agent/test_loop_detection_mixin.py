"""P9 pass-8 — LoopDetectionMixin extracted from service.py."""
import logging
import types

import pytest

from agents.task.agent.core.loop_detection import LoopDetectionMixin


def test_agent_composes_loop_detection_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, LoopDetectionMixin)
    for m in ("_check_if_stopped", "_trigger_loop_intervention", "detect_action_loop"):
        assert getattr(Agent, m).__qualname__.startswith("LoopDetectionMixin")


class _Host(LoopDetectionMixin):
    def __init__(self):
        self.logger = logging.getLogger("loop-test")
        self.state = types.SimpleNamespace(stopped=False, paused=False)


def test_check_if_stopped_passes_when_running():
    assert _Host()._check_if_stopped() is False


def test_check_if_stopped_raises_when_stopped():
    h = _Host()
    h.state.stopped = True
    with pytest.raises(InterruptedError):
        h._check_if_stopped()


def test_detect_action_loop_noop_without_context():
    h = _Host()
    h.task_context_manager = None
    h.session_id = None
    assert h.detect_action_loop(object(), [{"write_file": {}}]) == (False, None)
