"""A run that adds nothing new is warned, refused, then ended, even when the
arguments change on each step (prod 2026-11-06: 25 reads of 2 files at changing
offsets and 6 owner messages, with no identical call)."""
import logging
from collections import deque

from agents.task.agent.core.loop_detection import (
    STALE_BLOCK_AT, STALE_WARN_AT, LoopDetectionMixin)
from agents.task.agent.views import ActionResult


class _Action:
    def __init__(self, name, **params):
        self._d = {name: params}

    def model_dump(self, **_):
        return self._d


class _Output:
    def __init__(self, *actions):
        self.action = list(actions)
        self.current_state = None


class _Controller:
    def __init__(self, *names):
        self._names = names

    def get_action_names(self):
        return list(self._names)


class _Messages:
    def __init__(self):
        self.added = []

    def add_message(self, message):
        self.added.append(message)


class _Agent(LoopDetectionMixin):
    def __init__(self, *tools):
        self.logger = logging.getLogger("t")
        self._previous_actions = deque(maxlen=25)
        self._action_repetition_counter = 0
        self._max_allowed_repetitions = 99
        self.message_manager = _Messages()
        self.controller = _Controller(*tools)
        self._last_result = []
        self._reset_loop_guard()

    def step(self, *actions):
        """One step: the guard, then the record, as the step path runs them."""
        output = _Output(*actions)
        refused = self._loop_guard_check(output)
        self._last_result = refused or [ActionResult(extracted_content="ok")]
        self._record_action_for_loop_detection(output)
        return refused


def _read(offset, path="a.md"):
    return _Action("filesystem_read_file", filePath=path, offset=offset)


def test_a_read_walk_over_two_files_is_warned_refused_and_ended():
    agent = _Agent()
    offsets = iter(range(1000))
    agent.step(_read(0))
    agent.step(_read(0, "b.md"))
    for i in range(STALE_BLOCK_AT):
        assert agent.step(_read(next(offsets), "ab"[i % 2] + ".md")) is None
    assert len(agent.message_manager.added) == 1

    refused = agent.step(_read(next(offsets)))
    assert "STEP NOT RUN" in refused[0].error
    assert getattr(agent, "_loop_stop_text", None) is None

    ended = agent.step(_read(next(offsets)))
    assert "RUN ENDED" in ended[0].error
    assert agent._loop_stop_text


def test_a_new_target_restarts_the_count():
    agent = _Agent()
    for i in range(STALE_WARN_AT + STALE_BLOCK_AT):
        assert agent.step(_read(i, f"{i // (STALE_WARN_AT - 1)}.md")) is None
    assert not agent.message_manager.added


def test_a_write_after_a_refusal_runs_and_clears_the_refusal():
    agent = _Agent()
    agent.step(_read(0))
    for i in range(1, STALE_BLOCK_AT + 1):
        agent.step(_read(i))
    assert agent.step(_read(99)) is not None
    assert agent.step(_Action("filesystem_write_file", filePath="out", content="x")) is None
    assert agent._stale_steps == 0 and agent._stale_blocked is False
    assert agent.step(_read(100)) is None


def test_appends_with_new_content_are_progress():
    agent = _Agent()
    for i in range(STALE_BLOCK_AT * 2):
        assert agent.step(_Action("filesystem_append_file", filePath="log", content=str(i))) is None
    assert agent._stale_steps == 0


def test_browser_and_wait_steps_never_count():
    agent = _Agent()
    agent.step(_read(0))
    for _ in range(STALE_BLOCK_AT * 2):
        agent.step(_Action("browser_navigate", url="https://example.test"))
        agent.step(_Action("process_wait", seconds=5))
    assert agent._stale_steps == 0


def test_a_reread_between_sends_does_not_hide_them():
    agent = _Agent()
    agent.step(_read(0))
    refused = None
    for i in range(1, 6):
        agent.step(_read(i))
        refused = agent.step(_Action("message", target="owner", text=f"status {i}"))
        if refused:
            break
    assert refused and "MESSAGE NOT SENT" in refused[0].error


def test_new_work_between_sends_restarts_the_send_count():
    agent = _Agent()
    for i in range(10):
        assert agent.step(_read(0, f"{i}.md")) is None
        assert agent.step(_Action("message", target="owner", text=f"found {i}")) is None


def test_a_false_tool_claim_is_refused_and_a_tried_tool_is_not():
    agent = _Agent("filesystem_write_file")
    text = "I have no file-write tool in this session."
    refused = agent.step(_Action("message", target="owner", text=text))
    assert "filesystem_write_file" in refused[0].error

    agent = _Agent("filesystem_write_file")
    output = _Output(_Action("filesystem_write_file", filePath="x", content="y"))
    agent._last_result = [ActionResult(error="disk full")]
    agent._record_action_for_loop_detection(output)
    assert agent.step(_Action("message", target="owner", text=text)) is None


def test_the_claim_refusal_is_bounded():
    agent = _Agent("filesystem_write_file")
    text = "I have no file-write tool."
    assert agent.step(_Action("send_message", text=text)) is not None
    assert agent.step(_Action("send_message", text=text)) is not None
    assert agent.step(_Action("send_message", text=text)) is None
