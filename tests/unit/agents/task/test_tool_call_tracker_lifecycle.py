"""098 AG-18: ToolCallTracker — pending -> completed | failed | abandoned."""
import json

import pytest

from agents.task.agent import tool_call_tracker as T
from agents.task.agent.tool_call_tracker import TOOL_CALL_MACHINE, ToolCallTracker


@pytest.fixture
def saves(monkeypatch):
    calls = []
    monkeypatch.setattr(ToolCallTracker, "save_to_file", lambda self, p: calls.append(p) or True)
    return calls


def _t():
    return ToolCallTracker(session_id="s-tct")


def test_machine_well_formed():
    assert TOOL_CALL_MACHINE.validate() == []
    assert "executing" not in TOOL_CALL_MACHINE.states


def test_complete_step_abandons_unpaired_calls(saves):
    t = _t()
    t.register_tool_calls([{"id": "a", "name": "x"}, {"id": "b", "name": "y"}])
    t.mark_completed("a")
    t.complete_step()
    assert not t.has_active_calls()
    assert t._completed_calls["b"].status == "abandoned"
    assert t.get_statistics()["abandoned_calls"] == 1


def test_completed_calls_bounded(saves, monkeypatch):
    monkeypatch.setattr(T, "MAX_COMPLETED_CALLS", 5)
    monkeypatch.setattr(T, "MAX_STEP_HISTORY", 3)
    t = _t()
    for i in range(20):
        t.register_tool_calls([{"id": f"c{i}", "name": "x"}])
        t.mark_completed(f"c{i}")
        t.complete_step()
    assert len(t._completed_calls) == 5 and "c19" in t._completed_calls
    assert len(t._step_history) == 3


def test_save_only_when_dirty(saves):
    t = _t()
    t.complete_step()
    assert saves == []                       # nothing changed
    t.register_tool_calls([{"id": "a", "name": "x"}])
    t.mark_completed("a")
    t.complete_step()
    t.complete_step()
    t.complete_step()
    assert len(saves) == 1                   # once per real change, not 12x per step


def test_parent_and_child_use_distinct_files(saves):
    parent = ToolCallTracker(session_id="s-tct", user_id="u1")
    child = ToolCallTracker(session_id="s-tct", user_id="u1", file_name="tool_calls_sub_1.json")
    for t in (parent, child):
        t.register_tool_calls([{"id": "a", "name": "x"}])
        t.complete_step()
    assert saves[0].name == "tool_calls.json" and saves[1].name == "tool_calls_sub_1.json"


def test_restore_marks_active_calls_abandoned(tmp_path):
    f = tmp_path / "tool_calls.json"
    f.write_text(json.dumps({"version": "1.0", "session_id": "s-tct", "active_calls": {
        "z": {"id": "z", "name": "x", "args": {}, "timestamp": "2026-11-02T10:00:00",
              "status": "pending"}}, "current_step_calls": ["z"], "stats": {}}))
    t = _t()
    assert t.load_from_file(f)
    assert not t.has_active_calls() and t._completed_calls["z"].status == "abandoned"
