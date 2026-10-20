"""B2 (2026-10-11) — autonomous-run retention.

Prod: 25,437 rows, 84 days old at most, a 365-day window — the curator never
deleted a row and never said so. Rows a cron/goal run wrote now age out after
45 days; chat rows keep the long window; the curator logs the count every pass.
"""
import logging
import time

import pytest

from core.sqlite_util import execute_retry
from modules.memory import retention
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

NOW = int(time.time())
DAY = 86400


@pytest.fixture()
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


def _episode(provider, session_id, kind, user="u"):
    execute_retry(provider.db_path,
                  "INSERT INTO episodes (ts, user_id, session_id, kind, created_at) "
                  "VALUES (?,?,?,?,?)", (NOW, user, session_id, kind, NOW))


def _raw_row(provider, content, *, session_id, ts, user="u"):
    """Insert a row with a chosen provenance timestamp."""
    import hashlib
    rowid = execute_retry(provider.db_path,
                          "INSERT INTO memories (user_id, session_id, content) VALUES (?,?,?)",
                          (user, session_id, content), fetch="lastrowid")
    if rowid is None:
        rowid = execute_retry(provider.db_path, "SELECT MAX(rowid) AS r FROM memories",
                              fetch="one")["r"]
    execute_retry(provider.db_path,
                  "INSERT INTO mem_provenance (mem_rowid, user_id, ts, kind, content_hash, scope) "
                  "VALUES (?,?,?,?,?,'')",
                  (rowid, user, ts, "finding", hashlib.sha256(content.encode()).hexdigest()))
    return rowid


def _contents(provider):
    return sorted(r["content"] for r in execute_retry(
        provider.db_path, "SELECT content FROM memories", fetch="all"))


def test_autonomous_rows_age_out_sooner_chat_rows_stay(provider):
    _episode(provider, "cron-s", "cron")
    _episode(provider, "goal-s", "goal")
    _episode(provider, "chat-s", "chat")
    old = NOW - 60 * DAY
    _raw_row(provider, "cron finding sixty days", session_id="cron-s", ts=old)
    _raw_row(provider, "goal finding sixty days", session_id="goal-s", ts=old)
    _raw_row(provider, "chat finding sixty days", session_id="chat-s", ts=old)
    _raw_row(provider, "no episode finding sixty days", session_id="other", ts=old)
    _raw_row(provider, "cron finding fresh", session_id="cron-s", ts=NOW - 5 * DAY)
    removed = provider.prune_memories(older_than_ts=NOW - 365 * DAY,
                                      autonomous_older_than_ts=NOW - 45 * DAY)
    assert removed == 2
    assert _contents(provider) == ["chat finding sixty days", "cron finding fresh",
                                   "no episode finding sixty days"]
    prov = execute_retry(provider.db_path, "SELECT COUNT(*) AS n FROM mem_provenance",
                         fetch="one")
    assert prov["n"] == 3


def test_prune_without_autonomous_cutoff_is_unchanged(provider):
    _episode(provider, "cron-s", "cron")
    _raw_row(provider, "cron finding sixty days", session_id="cron-s", ts=NOW - 60 * DAY)
    assert provider.prune_memories(older_than_ts=NOW - 365 * DAY) == 0


def test_autonomous_window_is_capped_by_general_window():
    assert retention.autonomous_window_days(365) == 45
    assert retention.autonomous_window_days(30) == 30


def _curator():
    from agents.task.agent.core.curator import SkillCurator

    class _Usage:
        def list_authored(self, **kw):
            return []

        def get_state(self, key):
            return None

        def set_state(self, key, value):
            pass

    return SkillCurator(object(), _Usage())


@pytest.mark.asyncio
async def test_curator_prunes_autonomous_rows_and_logs_count(provider, monkeypatch, caplog):
    import modules.memory.registry as reg
    reg.reset_memory_registry()
    reg.set_external_memory_provider(provider)
    try:
        monkeypatch.setenv("MEMORY_RETENTION_DAYS", "365")
        _episode(provider, "cron-s", "cron")
        _raw_row(provider, "cron finding sixty days", session_id="cron-s", ts=NOW - 60 * DAY)
        with caplog.at_level(logging.INFO, logger="agents.task.agent.core.curator"):
            await _curator().run_once()
        assert _contents(provider) == []
        assert "pruned 1 old memories (window 365d, autonomous runs 45d)" in caplog.text

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="agents.task.agent.core.curator"):
            await _curator().run_once()
        assert "pruned 0 old memories" in caplog.text  # a zero is logged too
    finally:
        reg.reset_memory_registry()
