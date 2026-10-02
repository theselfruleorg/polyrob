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


# --- F12: interventions name only REGISTERED actions --------------------------

class _MM:
    def __init__(self):
        self.added = []

    def add_message(self, msg):
        self.added.append(msg)


def _intervene(names, reason):
    h = _Host()
    h.state = types.SimpleNamespace(stopped=False, paused=False, loop_warning_count=0,
                                    reset_loop_detection=lambda: None)
    h.controller = types.SimpleNamespace(get_action_names=lambda: list(names))
    h.message_manager = _MM()
    h._previous_actions = []
    h._action_repetition_counter = 0
    h._last_result = []
    h._trigger_loop_intervention(reason)
    return h.message_manager.added[-1].content


@pytest.mark.parametrize("reason", ["validation failed on arguments", "Action repeated 3 times"])
def test_intervention_omits_unregistered_tools(reason):
    text = _intervene(["done", "browser_navigate"], reason)
    assert "mcp_execute_tool" not in text
    assert "filesystem_write_file" not in text


def test_intervention_names_registered_tools():
    assert "mcp_execute_tool" in _intervene(["done", "mcp_execute_tool"], "validation failed")
    assert "filesystem_write_file" in _intervene(["done", "filesystem_write_file"],
                                                 "Action repeated 3 times")
