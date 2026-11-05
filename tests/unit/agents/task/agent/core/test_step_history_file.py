"""102 M-A, N-9: the step ledger appends to a file, and memory holds only its tail.

The whole ledger was rewritten on the loop at each step (114 ms at 2000 steps), the
in-memory list had no bound, and a recreated agent overwrote the earlier steps on disk.
The ⚠️ here is the index: ``since_steps`` and the chat baseline are ABSOLUTE.
"""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from agents.task.agent.core.history_io import STEP_HISTORY_KEEP, HistoryIOMixin
from agents.task.agent.views import AgentHistory, AgentHistoryList
from agents.task.loop_types.browser_views import BrowserState, BrowserStateHistory
from agents.task.loop_types.controller_types import ActionResult
from agents.task.runtime.run_outcome import _last_tool_error, absolute_step_count
from datagen.assemble import read_agent_steps

SESSION = "11111111-1111-4111-8111-111111111111"


class _Host(HistoryIOMixin):
    def __init__(self, sub=False):
        self.logger = logging.getLogger("step-history-test")
        self.history = AgentHistoryList(history=[])
        self.session_id, self.user_id, self.agent_name = SESSION, "u1", "executor"
        self._is_sub_agent = sub
        self.state = SimpleNamespace(n_steps=1)


def _state():
    return BrowserState(url="", title="", tabs=[], element_tree=None, selector_map={}, screenshot=None)


def _item(error=None, text="x"):
    return AgentHistory(
        model_output=None, result=[ActionResult(extracted_content=text, error=error)],
        state=BrowserStateHistory(url="", title="", tabs=[], interacted_element=[None]))


@pytest.fixture
def history_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.task.agent.core.history_io.pm",
                        lambda: SimpleNamespace(get_history_dir=lambda s, u: tmp_path))
    return tmp_path


def _step(host, text):
    asyncio.run(host._make_history_item(None, _state(), [ActionResult(extracted_content=text)]))


def test_each_step_appends_one_line_and_leaves_the_earlier_bytes(history_dir):
    host = _Host()
    _step(host, "one")
    path = history_dir / "agent_history_executor.jsonl"
    first = path.read_bytes()
    _step(host, "two")
    second = path.read_bytes()
    assert second.startswith(first), "append-only: an earlier step never changes"
    assert len(second) < 2.2 * len(first), "the size is linear in steps"
    assert [json.loads(l)["result"][0]["extracted_content"] for l in second.splitlines()] == ["one", "two"]
    assert not (history_dir / "agent_history_executor.json").exists()


def test_a_recreated_agent_keeps_the_earlier_steps(history_dir):
    _step(_Host(), "before the eviction")
    _step(_Host(), "after the recreate")
    lines = (history_dir / "agent_history_executor.jsonl").read_text().splitlines()
    assert [json.loads(l)["result"][0]["extracted_content"] for l in lines] == [
        "before the eviction", "after the recreate"]


def test_the_only_reader_reads_both_forms_in_order_and_survives_a_torn_line(tmp_path):
    hist = tmp_path / "data" / "history"
    hist.mkdir(parents=True)
    (hist / "agent_history_a.json").write_text(json.dumps({"history": [{"n": 1}]}))
    (hist / "agent_history_a.jsonl").write_text('{"n": 2}\n{"n": 3}\n{"n": 4')
    assert read_agent_steps(tmp_path) == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_trim_keeps_the_tail_and_the_absolute_count():
    host = _Host()
    host.history.history.extend(_item(text=str(i)) for i in range(STEP_HISTORY_KEEP + 7))
    before = absolute_step_count(host.history)
    host.trim_step_history()
    assert len(host.history.history) == STEP_HISTORY_KEEP
    assert host.history.trimmed_steps == 7
    assert absolute_step_count(host.history) == before
    assert host.history.history[-1].result[0].extracted_content == str(STEP_HISTORY_KEEP + 6)


def test_a_sub_agent_keeps_its_whole_run():
    host = _Host(sub=True)
    host.history.history.extend(_item() for _ in range(STEP_HISTORY_KEEP + 7))
    host.trim_step_history()
    assert len(host.history.history) == STEP_HISTORY_KEEP + 7


def _output():
    action = SimpleNamespace(model_dump=lambda **k: {"x": {}})
    return SimpleNamespace(action=[action], current_state=None)


def test_since_steps_stays_an_absolute_index_across_a_trim():
    host = _Host()
    old = SimpleNamespace(model_output=_output(), result=[SimpleNamespace(error="an old turn's error")])
    host.history = SimpleNamespace(history=[old] * 10, trimmed_steps=0)
    agent = SimpleNamespace(history=host.history)
    since = absolute_step_count(host.history)   # the baseline, before the run

    host.history.history = host.history.history[-4:]  # the run start trims
    host.history.trimmed_steps = 6
    assert _last_tool_error(agent, since) == "", "nothing new yet, and the old error is not this turn's"

    new = SimpleNamespace(model_output=_output(), result=[SimpleNamespace(error="this turn's error")])
    host.history.history.append(new)
    assert _last_tool_error(agent, since) == "this turn's error"


def test_the_chat_baseline_sees_a_new_step_after_a_trim():
    from agents.task_agent_lite import TaskAgent
    host = _Host()
    host.history.history.extend(_item(text="old") for _ in range(10))
    a = object.__new__(TaskAgent)
    a._registry = SimpleNamespace(get=lambda sid: SimpleNamespace(agents={"x": host}))
    host.message_manager = None
    baseline = a._chat_turn_baseline("s")
    assert baseline[0] == 10

    host.history.trim(4)
    assert a._extract_chat_reply("s", since=baseline) in ("", None), "no new step yet: no reply"
    done = _item(text="this turn's answer")
    done.result[0].is_done = True
    host.history.history.append(done)
    assert absolute_step_count(host.history) > baseline[0] > len(host.history.history)
    assert a._extract_chat_reply("s", since=baseline) == "this turn's answer"
