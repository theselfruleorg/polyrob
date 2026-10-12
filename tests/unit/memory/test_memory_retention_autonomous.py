"""B2 (2026-10-11) — autonomous-run retention + the one-time noise purge.

Prod: 25,437 rows, 84 days old at most, a 365-day window — the curator never
deleted a row and never said so. Rows a cron/goal run wrote now age out after
45 days; chat rows keep the long window; the curator logs the count every
pass; the rows the write filter now refuses are purged once.
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
    """Insert a row as an OLD write would have (bypasses the write filter)."""
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


def test_protected_autonomous_rows_keep_the_long_window(provider):
    _episode(provider, "goal-s", "goal")
    _raw_row(provider, "Owner clarified: 'you did not notify x and chat'. Post on both.",
             session_id="goal-s", ts=NOW - 60 * DAY)
    _raw_row(provider, "Step 3: read the ledger.\nLesson: reconcile refuses files over 1MB.",
             session_id="goal-s", ts=NOW - 60 * DAY)
    _raw_row(provider, "Read the ledger tail.", session_id="goal-s", ts=NOW - 60 * DAY)
    removed = provider.prune_memories(older_than_ts=NOW - 365 * DAY,
                                      autonomous_older_than_ts=NOW - 45 * DAY)
    assert removed == 1
    assert "Read the ledger tail." not in _contents(provider)
    # the long window still applies to them
    assert provider.prune_memories(older_than_ts=NOW - 30 * DAY) == 2


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
async def test_curator_prunes_autonomous_rows_logs_count_and_purges_noise(
        provider, monkeypatch, caplog):
    import modules.memory.registry as reg
    reg.reset_memory_registry()
    reg.set_external_memory_provider(provider)
    try:
        monkeypatch.setenv("MEMORY_RETENTION_DAYS", "365")
        _episode(provider, "cron-s", "cron")
        _raw_row(provider, "cron finding sixty days", session_id="cron-s", ts=NOW - 60 * DAY)
        _raw_row(provider, "Planning turn done. Now reading.", session_id="chat-s", ts=NOW)
        with caplog.at_level(logging.INFO, logger="agents.task.agent.core.curator"):
            await _curator().run_once()
        assert _contents(provider) == []
        text = caplog.text
        assert "pruned 1 old memories (window 365d, autonomous runs 45d)" in text
        assert "memory noise purge" in text

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="agents.task.agent.core.curator"):
            await _curator().run_once()
        assert "pruned 0 old memories" in caplog.text  # a zero is logged too
        assert "memory noise purge" not in caplog.text  # one-time: marker set
    finally:
        reg.reset_memory_registry()


def test_noise_purge_deletes_rewrites_and_keeps_fts_consistent(provider):
    clue = ("[STRATEGIC CLUE FROM DISCOVERY] Discovery: 3 insights. Proceed with "
            "verified tools and methods.")
    _raw_row(provider, clue, session_id="s", ts=NOW)
    _raw_row(provider, "filesystem_read_file(file_path=a.md)→zebra contents", session_id="s",
             ts=NOW)
    mixed = _raw_row(provider, clue + "\nOwner approved giraffe buying.", session_id="s",
                     ts=NOW)
    _raw_row(provider, "Reconcile verdict: all rows match chain.", session_id="s", ts=NOW)

    got = retention.purge_noise_rows(provider)
    assert got == {"scanned": 4, "deleted": 2, "rewritten": 1}
    assert _contents(provider) == ["Owner approved giraffe buying.",
                                   "Reconcile verdict: all rows match chain."]

    def match(term):
        return execute_retry(provider.db_path,
                             "SELECT rowid FROM memories WHERE memories MATCH ?",
                             (f'"{term}"',), fetch="all")
    assert match("zebra") == [] and match("STRATEGIC") == [] and match("Proceed") == []
    assert [r["rowid"] for r in match("giraffe")] == [mixed]
    # the provenance hash follows the rewrite, so a re-write of the clean text dedups
    import hashlib
    h = execute_retry(provider.db_path, "SELECT content_hash FROM mem_provenance "
                      "WHERE mem_rowid = ?", (mixed,), fetch="one")["content_hash"]
    assert h == hashlib.sha256(b"Owner approved giraffe buying.").hexdigest()
    orphans = execute_retry(provider.db_path,
                            "SELECT COUNT(*) AS n FROM mem_provenance p WHERE NOT EXISTS "
                            "(SELECT 1 FROM memories m WHERE m.rowid = p.mem_rowid)",
                            fetch="one")
    assert orphans["n"] == 0

    # idempotent: the marker makes the second call a no-op
    assert retention.purge_noise_rows(provider) == {"scanned": 0, "deleted": 0,
                                                    "rewritten": 0, "skipped": 1}
    assert retention.purge_noise_rows(provider, force=True)["deleted"] == 0


def test_noise_purge_writes_a_backup_first(provider):
    import os
    import sqlite3
    _raw_row(provider, "Planning turn done. Now reading.", session_id="s", ts=NOW)
    got = retention.purge_noise_rows(provider)
    assert got["deleted"] == 1
    bak = retention.backup_path(provider.db_path)
    assert bak.endswith(f"bak-purge-v{retention.write_filter.CLASSIFIER_VERSION}")
    con = sqlite3.connect(bak)
    try:
        assert [r[0] for r in con.execute("SELECT content FROM memories")] == [
            "Planning turn done. Now reading."]
    finally:
        con.close()
    assert os.path.exists(bak)


def test_noise_purge_skips_when_backup_fails(provider, monkeypatch):
    _raw_row(provider, "Planning turn done. Now reading.", session_id="s", ts=NOW)
    monkeypatch.setattr(retention, "backup_path",
                        lambda db: "/nonexistent-dir/xyz/memory.db.bak")
    got = retention.purge_noise_rows(provider)
    assert got.get("backup_failed") == 1 and got["deleted"] == 0
    assert _contents(provider) == ["Planning turn done. Now reading."]
    assert not retention.noise_purge_done(provider.db_path)


def test_noise_purge_handles_more_than_one_batch(provider, monkeypatch):
    monkeypatch.setattr(retention, "_BATCH", 3)
    for i in range(7):
        _raw_row(provider, f"Planning turn done. Now step {i}.", session_id="s", ts=NOW)
    _raw_row(provider, "Lesson: keep me.", session_id="s", ts=NOW)
    got = retention.purge_noise_rows(provider)
    assert got["scanned"] == 8 and got["deleted"] == 7
    assert _contents(provider) == ["Lesson: keep me."]
