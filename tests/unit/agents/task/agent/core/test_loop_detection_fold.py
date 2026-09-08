"""S7 (2026-08-29): the hash-based action-repetition detector lives on
``LoopDetectionMixin._record_action_for_loop_detection`` (folded in from the step
recording tail) and honours the configured repetition threshold + A-B-A-B pattern."""
import logging
from collections import deque

from agents.task.agent.core.loop_detection import LoopDetectionMixin


class _Action:
    def __init__(self, name, **params):
        self._d = {name: params}

    def model_dump(self, **_):
        return self._d


class _Output:
    def __init__(self, *actions):
        self.action = list(actions)
        self.current_state = None


class _Agent(LoopDetectionMixin):
    def __init__(self, max_reps=2):
        self.logger = logging.getLogger("t")
        self._previous_actions = deque(maxlen=25)
        self._action_repetition_counter = 0
        self._max_allowed_repetitions = max_reps
        self.interventions = []

    def _trigger_loop_intervention(self, reason, clear_history=False):
        self.interventions.append(reason)


def test_same_action_repeated_hits_threshold():
    a = _Agent(max_reps=2)
    for _ in range(3):
        a._record_action_for_loop_detection(_Output(_Action("click", index=1)))
    assert any("repeated" in r for r in a.interventions)


def test_param_order_does_not_matter_and_different_action_resets():
    a = _Agent(max_reps=2)
    a._record_action_for_loop_detection(_Output(_Action("go", a=1, b=2)))
    a._record_action_for_loop_detection(_Output(_Action("go", b=2, a=1)))  # same hash
    assert a._action_repetition_counter == 1
    a._record_action_for_loop_detection(_Output(_Action("scroll")))
    assert a._action_repetition_counter == 0


def test_alternating_pattern_intervenes():
    a = _Agent(max_reps=99)
    for name in ("a", "b", "a", "b"):
        a._record_action_for_loop_detection(_Output(_Action(name)))
    assert any("A-B-A-B" in r for r in a.interventions)
