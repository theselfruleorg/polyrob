"""The numbered element list reaches the prompt only when `_has_active_browser_usage`
says the browser is in use. Both of its signals were dead (prod 2026-10-04 11:02):

- check 1 scanned `_previous_actions` for names like `browser_go_to`, but that deque
  holds MD5 hashes (loop detection), so it never matched;
- check 2 read `_last_browser_states`, which nothing ever appended to.

So the agent navigated and read pages but never saw element indices, guessed
`browser_input_text(index=0)` and could not fill a single form.
"""
from collections import deque
from types import SimpleNamespace

from agents.task.agent.core.loop_detection import LoopDetectionMixin
from agents.task.agent.core.step import _note_browser_state
from agents.task.agent.service import Agent


class _Action:
    def __init__(self, name, params):
        self._name, self._params = name, params

    def model_dump(self, **_):
        return {self._name: self._params}


class _Probe(LoopDetectionMixin):
    _has_active_browser_usage = Agent._has_active_browser_usage

    def __init__(self):
        self._previous_actions = deque(maxlen=10)
        self._recent_action_names = deque(maxlen=10)
        self._last_browser_states = deque(maxlen=3)
        self._action_repetition_counter = 0
        self._max_allowed_repetitions = 99
        self.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)

    def _trigger_loop_intervention(self, *_):
        pass


def _state(url, title="Page"):
    return SimpleNamespace(url=url, title=title)


def test_fresh_agent_is_not_using_the_browser():
    assert _Probe()._has_active_browser_usage() is False


def test_a_recorded_browser_action_counts_as_usage():
    probe = _Probe()
    out = SimpleNamespace(action=[_Action("browser_go_to_url", {"url": "https://x.test"})])
    probe._record_action_for_loop_detection(out)
    assert probe._has_active_browser_usage() is True


def test_any_browser_action_counts_not_just_a_short_prefix_list():
    probe = _Probe()
    out = SimpleNamespace(action=[_Action("browser_input_text", {"index": 3, "text": "a"})])
    probe._record_action_for_loop_detection(out)
    assert probe._has_active_browser_usage() is True


def test_a_non_browser_action_does_not_count():
    probe = _Probe()
    out = SimpleNamespace(action=[_Action("filesystem_write_file", {"file_path": "a"})])
    probe._record_action_for_loop_detection(out)
    assert probe._has_active_browser_usage() is False


def test_a_real_page_is_remembered_and_counts_as_usage():
    probe = _Probe()
    _note_browser_state(probe, _state("https://www.coincatapult.com/register"))
    assert probe._has_active_browser_usage() is True


def test_placeholder_states_are_not_remembered():
    probe = _Probe()
    for st in (_state(""), _state("about:blank"), _state("", "No Browser"), None):
        _note_browser_state(probe, st)
    assert len(probe._last_browser_states) == 0
    assert probe._has_active_browser_usage() is False
