# tests/unit/agents/task/test_recall_query_no_memory.py
#
# BUG 3 fix verification: _build_recall_query previously read the wrong attribute
# names (state.current_brain / _last_brain / .next — all non-existent), so `brain`
# was always None and `nxt` was always "", making the recall query permanently just
# the task string.
#
# The real attribute is self._last_brain_state (set in next_action_internal.py:646),
# holding an AgentBrain whose relevant field is `next_goal` (views.py:39).
# The `memory` field must NEVER enter the query (T4 invariant).

from types import SimpleNamespace
from agents.task.agent.core.memory_prefetch import MemoryPrefetchMixin


class _Stub(MemoryPrefetchMixin):
    """Minimal stub that owns the real _last_brain_state attribute."""
    def __init__(self, task, brain_state):
        self.task = task
        self._last_brain_state = brain_state


def test_recall_query_ignores_brain_memory():
    """The `memory` field on AgentBrain must NEVER appear in the recall query."""
    brain = SimpleNamespace(next_goal="open the dashboard", memory="OLD STALE RULES from 2 days ago")
    q = _Stub("do the task", brain)._build_recall_query()
    assert "STALE" not in q
    assert "OLD" not in q
    # next_goal SHOULD be included
    assert "open the dashboard" in q
    assert "do the task" in q


def test_recall_query_uses_next():
    """next_goal value should be included in the recall query (the bug made it always empty)."""
    brain = SimpleNamespace(next_goal="open the dashboard", memory="stale")
    q = _Stub("do the task", brain)._build_recall_query()
    assert "open the dashboard" in q
    assert "stale" not in q


def test_recall_query_no_brain_state_falls_back_to_task():
    """When _last_brain_state is None the query is just the task string."""
    q = _Stub("do the task", None)._build_recall_query()
    assert q == "do the task"


def test_recall_query_dict_brain_uses_next_goal():
    """dict-form brain with next_goal key (robustness path)."""
    brain = {"next_goal": "check the logs", "memory": "ignored memory field"}
    q = _Stub("analyse this", brain)._build_recall_query()
    assert "check the logs" in q
    assert "ignored" not in q


def test_recall_query_empty_task_and_next_goal():
    """Empty task + empty next_goal → empty-string handled gracefully."""
    brain = SimpleNamespace(next_goal="", memory="sensitive")
    q = _Stub("", brain)._build_recall_query()
    assert "sensitive" not in q
