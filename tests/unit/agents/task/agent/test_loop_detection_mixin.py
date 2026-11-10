"""P9 pass-8 — LoopDetectionMixin extracted from service.py."""
import logging
import types

import pytest

from agents.task.agent.core.loop_detection import LoopDetectionMixin


def test_agent_composes_loop_detection_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, LoopDetectionMixin)
    for m in ("_check_if_stopped", "_trigger_loop_intervention", "_record_progress"):
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


def test_reset_clears_pattern_window():
    """098 AG-04: the A-B-A-B window does not span two runs."""
    from collections import deque
    from agents.task.agent.core.loop_detection import LoopDetectionMixin

    h = types.SimpleNamespace(_previous_actions=deque(["A", "B"], maxlen=8))
    LoopDetectionMixin._reset_loop_guard(h)
    assert list(h._previous_actions) == []


def test_intervention_error_is_not_hidden_in_a_committed_result_list():
    """The step's result list is marked as sent in ToolMessages by identity (F14); the
    loop error must go on a new list, or the next state message never shows it."""
    from agents.task.agent.views import ActionResult
    h = _Host()
    h.state = types.SimpleNamespace(stopped=False, paused=False, loop_warning_count=0,
                                    reset_loop_detection=lambda: None)
    h.controller = types.SimpleNamespace(get_action_names=lambda: ["done"])
    h.message_manager = _MM()
    h._previous_actions = []
    h._action_repetition_counter = 0
    committed = [ActionResult(extracted_content="same result")]
    h._last_result = committed
    h._trigger_loop_intervention("Action repeated 3 times")
    assert h._last_result is not committed
    assert len(committed) == 1
    assert "LOOP DETECTED" in (h._last_result[-1].error or "")
