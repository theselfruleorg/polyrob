"""Regression (P1): TaskContextManager._sessions is a process-global dict keyed by
session_id ALONE. load_session's cache-hit path returned the cached HierarchicalMemory
regardless of user_id, so a session_id reused across tenants (permitted upstream after
the old owner is reaped) leaked tenant A's warm H-MEM to tenant B. The cache hit must
verify the requesting user_id.
"""
import tempfile

import pytest
from unittest.mock import MagicMock

from modules.memory.task.task_context_manager import TaskContextManager


class _Cfg:
    def __init__(self, values):
        self.data = dict(values)

    def get(self, key, default=None):
        return self.data.get(key, default)


def _mgr():
    d = tempfile.mkdtemp()
    cfg = _Cfg({
        "HIERARCHICAL_MEMORY_ENABLED": True,
        "SEMANTIC_RETRIEVAL_ENABLED": False,
        "DATA_DIR": d, "DATA_PATH": d,
    })
    m = TaskContextManager(name="t", config=cfg)
    stub = MagicMock()
    stub.has_service.return_value = True
    m.container = stub
    return m


@pytest.mark.asyncio
async def test_cache_hit_is_tenant_scoped():
    m = _mgr()
    await m.initialize()
    try:
        sid = "shared-session-id"
        # Tenant A creates the session → warms the process-global cache.
        m.create_session(sid, task="tenant A secret task", user_id="tenant_a")
        assert sid in m._sessions

        # Tenant B loads the SAME session_id. It must NOT receive tenant A's warm
        # memory from the cache; the stale entry is dropped and a per-tenant disk
        # load is attempted (which finds nothing → None here).
        got = m.load_session(sid, user_id="tenant_b")
        assert got is None, "tenant B must not get tenant A's cached H-MEM"

        # Tenant A still gets its own memory on a same-tenant load.
        m.create_session(sid, task="A again", user_id="tenant_a") if sid not in m._sessions else None
        same = m.load_session(sid, user_id="tenant_a")
        assert same is not None
    finally:
        await m.cleanup()
