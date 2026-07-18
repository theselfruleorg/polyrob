"""B3 — age-based retention for the cross-session `memories` store.

`DELETE FROM memories` existed nowhere: the store grew forever (GEM failure
mode #1). Retention rides the curator tick (like episode pruning), keyed on
the B2 provenance timestamp. Legacy rows without a provenance stamp are
age-exempt (their age is unknowable — deleting them would be a guess).
"""
from __future__ import annotations

import time

import pytest

from core.sqlite_util import execute_retry
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

USER = "user_ret"
OLD_TS = int(time.time()) - 400 * 86400  # ~400 days ago


@pytest.fixture()
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "false")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


async def _seed(provider, content: str, *, ts: int = None) -> None:
    await provider.sync_turn("q", content, session_id="s1", user_id=USER)
    if ts is not None:
        execute_retry(provider.db_path,
                      "UPDATE mem_provenance SET ts = ? WHERE mem_rowid = "
                      "(SELECT MAX(mem_rowid) FROM mem_provenance)", (ts,))


@pytest.mark.asyncio
async def test_prunes_old_stamped_rows(provider):
    await _seed(provider, "ancient finding", ts=OLD_TS)
    await _seed(provider, "fresh finding")
    removed = provider.prune_memories(older_than_ts=int(time.time()) - 365 * 86400)
    assert removed == 1
    contents = provider._keyword_contents("finding", norm_user=USER, limit=10)
    assert any("fresh" in c for c in contents)
    assert not any("ancient" in c for c in contents)
    # provenance row swept with its memory row
    prov = execute_retry(provider.db_path,
                         "SELECT COUNT(*) AS n FROM mem_provenance", fetch="all")
    assert prov[0]["n"] == 1


@pytest.mark.asyncio
async def test_legacy_stampless_rows_are_exempt(provider):
    execute_retry(provider.db_path,
                  "INSERT INTO memories (user_id, session_id, content) VALUES (?,?,?)",
                  (USER, "old", "legacy row of unknowable age"))
    removed = provider.prune_memories(older_than_ts=int(time.time()) + 10)
    assert removed == 0
    contents = provider._keyword_contents("legacy", norm_user=USER, limit=10)
    assert contents


@pytest.mark.asyncio
async def test_prune_failopen(provider, monkeypatch, tmp_path):
    # A broken DB path degrades to 0, never raises (curator-tick contract).
    provider.db_path = str(tmp_path / "nonexistent-dir" / "nope.db")
    assert provider.prune_memories(older_than_ts=int(time.time())) == 0


@pytest.mark.asyncio
async def test_curator_runs_memory_prune(monkeypatch, provider):
    """The curator tick sweeps memories with the MEMORY_RETENTION_DAYS cutoff,
    and a <=0 window disables the sweep."""
    import modules.memory.registry as reg
    from agents.task.agent.core.curator import SkillCurator

    reg.reset_memory_registry()
    reg.set_external_memory_provider(provider)
    try:
        await _seed(provider, "ancient finding", ts=OLD_TS)

        class _Usage:
            def list_authored(self, **kw):
                return []
            def get_state(self, key):
                return None
            def set_state(self, key, value):
                pass

        curator = SkillCurator(object(), _Usage())
        monkeypatch.setenv("MEMORY_RETENTION_DAYS", "365")
        await curator.run_once()
        rows = execute_retry(provider.db_path,
                             "SELECT COUNT(*) AS n FROM memories", fetch="all")
        assert rows[0]["n"] == 0

        await _seed(provider, "ancient finding two", ts=OLD_TS)
        monkeypatch.setenv("MEMORY_RETENTION_DAYS", "0")
        await curator.run_once()
        rows = execute_retry(provider.db_path,
                             "SELECT COUNT(*) AS n FROM memories", fetch="all")
        assert rows[0]["n"] == 1  # disabled — nothing swept
    finally:
        reg.reset_memory_registry()
