"""M1: TaskContextManager is a process-wide singleton; session eviction/cleanup only
called save_session (persist to disk) and never close_session (the only method that
removes the in-memory HierarchicalMemory entry). close_session was dead code, so
_sessions grew unbounded on a long-running server / the autonomous goal-cron loop.
This guards that close_session actually reclaims the entry (cleanup.py now calls it).
"""
import tempfile
from unittest.mock import MagicMock

import pytest

from modules.memory.task.task_context_manager import TaskContextManager


class _Cfg:
    def __init__(self, values):
        self.data = dict(values)

    def get(self, key, default=None):
        return self.data.get(key, default)


def _mgr():
    data_dir = tempfile.mkdtemp()
    cfg = _Cfg({
        "HIERARCHICAL_MEMORY_ENABLED": True,
        "MAX_RECENT_STEPS": 20,
        "SEMANTIC_RETRIEVAL_ENABLED": False,
        "DATA_DIR": data_dir,
        "DATA_PATH": data_dir,
    })
    m = TaskContextManager(name="m", config=cfg)
    c = MagicMock()
    c.has_service.return_value = True
    m.container = c
    return m


@pytest.mark.asyncio
async def test_close_session_reclaims_in_memory_entry():
    m = _mgr()
    await m.initialize()
    try:
        m.create_session(session_id="s1", task="t", user_id="u1")
        assert "s1" in m._sessions

        ok = m.close_session(session_id="s1", user_id="u1")
        assert ok is True
        assert "s1" not in m._sessions  # entry reclaimed, not just persisted
    finally:
        await m.cleanup()
