"""M3: SqliteMemoryProvider.sync_turn/search/prefetch called the synchronous, blocking
execute_retry (which does real time.sleep retries under WAL contention — up to ~2s)
INLINE on the asyncio event loop. With MEMORY_BACKEND=sqlite default-on and multiple
concurrent sessions, that froze the whole loop. The blocking DB work must run off the
loop (run_in_executor), i.e. on a worker thread, not the event-loop thread.
"""
import threading

import pytest

from modules.memory import sqlite_memory_provider as mod
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


@pytest.mark.asyncio
async def test_sync_turn_runs_sqlite_off_the_event_loop(monkeypatch, tmp_path):
    provider = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    main_ident = threading.get_ident()
    seen = {}

    def _fake_execute_retry(*a, **k):
        seen["ident"] = threading.get_ident()
        return []

    monkeypatch.setattr(mod, "execute_retry", _fake_execute_retry)

    await provider.sync_turn("hi", "hello", session_id="s1", user_id="u1")

    assert "ident" in seen, "execute_retry was not called"
    assert seen["ident"] != main_ident, "blocking sqlite ran on the event-loop thread"


@pytest.mark.asyncio
async def test_search_runs_sqlite_off_the_event_loop(monkeypatch, tmp_path):
    provider = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    main_ident = threading.get_ident()
    seen = {}

    def _fake_execute_retry(*a, **k):
        seen["ident"] = threading.get_ident()
        return []

    monkeypatch.setattr(mod, "execute_retry", _fake_execute_retry)

    await provider.search("some meaningful query terms", user_id="u1", session_id="s1")

    assert seen.get("ident") not in (None, main_ident)
