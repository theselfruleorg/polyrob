"""§7.7 — session-close reflection trigger for the autonomous workload.

The per-step reflection trigger fires only at 25 findings in ONE session (an
in-memory counter that resets per session), so short cron/goal sessions never reach
it (reflection_consolidate=0 in prod). A session-close trigger consolidates a short
session's handful of findings at cleanup, at a lower threshold, so autonomous runs
actually reflect. Gated REFLECTION_ON_SESSION_CLOSE (default OFF).
"""
import tempfile
from unittest.mock import MagicMock

import pytest

from modules.memory.task.task_context_manager import TaskContextManager


class FakeConfig:
    def __init__(self, values: dict):
        self.data = dict(values)

    def get(self, key, default=None):
        return self.data.get(key, default)


def _make_manager(values):
    data_dir = tempfile.mkdtemp()
    base = {
        "HIERARCHICAL_MEMORY_ENABLED": True,
        "SEMANTIC_RETRIEVAL_ENABLED": False,
        "DATA_DIR": data_dir,
        "DATA_PATH": data_dir,
    }
    base.update(values)
    cfg = FakeConfig(base)
    mgr = TaskContextManager(name="test_manager", config=cfg)
    stub = MagicMock()
    stub.has_service.return_value = True
    mgr.container = stub
    return mgr


def _add_findings(mgr, session_id, n):
    for i in range(n):
        mgr.add_step_memory(
            session_id=session_id, step=i + 1,
            brain_state={"phase": "discovery", "memory": "x", "next_goal": "y"},
            action_summary=f"act {i}", finding=f"finding {i}", total_steps=50)


def test_close_triggers_reflection_below_step_threshold_when_enabled():
    mgr = _make_manager({"REFLECTION_ON_SESSION_CLOSE": True,
                         "REFLECTION_SESSION_CLOSE_THRESHOLD": 5})
    mgr.create_session(session_id="s1", task="t")
    _add_findings(mgr, "s1", 6)  # below the per-step threshold of 25
    mgr._trigger_reflection = MagicMock()
    assert mgr.close_session("s1") is True
    mgr._trigger_reflection.assert_called_once()


def test_close_no_reflection_when_flag_off():
    mgr = _make_manager({"REFLECTION_ON_SESSION_CLOSE": False,
                         "REFLECTION_SESSION_CLOSE_THRESHOLD": 5})
    mgr.create_session(session_id="s2", task="t")
    _add_findings(mgr, "s2", 6)
    mgr._trigger_reflection = MagicMock()
    mgr.close_session("s2")
    mgr._trigger_reflection.assert_not_called()


def test_close_no_reflection_below_min_findings():
    mgr = _make_manager({"REFLECTION_ON_SESSION_CLOSE": True,
                         "REFLECTION_SESSION_CLOSE_THRESHOLD": 5})
    mgr.create_session(session_id="s3", task="t")
    _add_findings(mgr, "s3", 2)  # fewer than the close threshold -> no cost
    mgr._trigger_reflection = MagicMock()
    mgr.close_session("s3")
    mgr._trigger_reflection.assert_not_called()
