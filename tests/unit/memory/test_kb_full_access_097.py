"""097 — the knowledge base has full access, and what the agent wrote ages out.

Owner decisions (2026-11-02): no review / promote step for the agent's own
documents (D1); a search reads EVERY collection unless one is named (D2); a
written document no search returned for 90 days leaves the KB (D3); retention
runs on its own always-on tick (D4).
"""
import os
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from modules.memory.local_vector_memory_provider import (
    LocalVectorMemoryProvider, _vec_available,
)
from modules.memory.sqlite_kb_store import RETIRED_AUTO_COLLECTION, WRITTEN_COLLECTION
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

USER = "owner"


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_REQUIRE_USER_ID", raising=False)
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


async def _put(p, collection, source, text, *, created_at=None):
    assert await p.kb_replace_source(
        user_id=USER, collection=collection, source_path=source,
        source_hash=f"h-{source}-{collection}", chunks=[text],
        created_at=created_at or datetime.now(timezone.utc).isoformat())


@pytest.mark.asyncio
async def test_search_without_a_collection_reads_every_collection(provider):
    await _put(provider, "default", "/a.md", "postgres tuning guide")
    await _put(provider, WRITTEN_COLLECTION, "/b.md", "postgres backup notes")
    out = await provider.kb_search("postgres", user_id=USER)
    assert "/a.md" in out and "/b.md" in out


@pytest.mark.asyncio
async def test_a_named_collection_still_filters(provider):
    await _put(provider, "default", "/a.md", "postgres tuning guide")
    await _put(provider, WRITTEN_COLLECTION, "/b.md", "postgres backup notes")
    out = await provider.kb_search("postgres", user_id=USER, collection="default")
    assert "/a.md" in out and "/b.md" not in out


@pytest.mark.asyncio
async def test_one_document_in_two_collections_answers_once(provider):
    await _put(provider, "default", "/a.md", "postgres tuning guide")
    await _put(provider, WRITTEN_COLLECTION, "/a.md", "postgres tuning guide")
    out = await provider.kb_search("postgres", user_id=USER)
    assert out.count("postgres tuning guide") == 1


@pytest.mark.asyncio
async def test_the_registry_default_is_every_collection(provider):
    import modules.memory.registry as reg
    reg.reset_memory_registry()
    reg.set_external_memory_provider(provider)
    try:
        await _put(provider, WRITTEN_COLLECTION, "/b.md", "kubernetes runbook")
        assert "/b.md" in await reg.kb_search("kubernetes", user_id=USER)
    finally:
        reg.reset_memory_registry()


def test_the_retired_auto_collection_is_renamed_in_place(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_REQUIRE_USER_ID", raising=False)
    db = str(tmp_path / "memory.db")
    SqliteMemoryProvider(db)                       # build the schema
    con = sqlite3.connect(db)
    con.execute("INSERT INTO kb_sources (user_id, collection, source_path, source_hash) "
                "VALUES (?,?,?,?)", (USER, RETIRED_AUTO_COLLECTION, "/old.md", "h"))
    con.execute("INSERT INTO kb_chunks (user_id, collection, source_path, chunk_idx, "
                "content) VALUES (?,?,?,?,?)",
                (USER, RETIRED_AUTO_COLLECTION, "/old.md", "0", "legacy text"))
    con.commit()
    con.close()

    SqliteMemoryProvider(db)                       # re-open: the rename runs
    con = sqlite3.connect(db)
    got = {r[0] for r in con.execute("SELECT collection FROM kb_sources")}
    chunk = {r[0] for r in con.execute("SELECT collection FROM kb_chunks")}
    con.close()
    assert got == chunk == {WRITTEN_COLLECTION}


def test_a_rollback_duplicate_under_the_old_name_does_not_stop_the_store(tmp_path, monkeypatch):
    """A rollback re-indexed a source under the old name after the rename: the
    next start keeps the new row and drops the old one (UNIQUE key)."""
    monkeypatch.delenv("MEMORY_REQUIRE_USER_ID", raising=False)
    db = str(tmp_path / "memory.db")
    SqliteMemoryProvider(db)
    con = sqlite3.connect(db)
    for coll, text in ((WRITTEN_COLLECTION, "new text"), (RETIRED_AUTO_COLLECTION, "old text")):
        con.execute("INSERT INTO kb_sources (user_id, collection, source_path, source_hash) "
                    "VALUES (?,?,?,?)", (USER, coll, "/doc.md", coll))
        con.execute("INSERT INTO kb_chunks (user_id, collection, source_path, chunk_idx, "
                    "content) VALUES (?,?,?,?,?)", (USER, coll, "/doc.md", "0", text))
    con.commit()
    con.close()

    SqliteMemoryProvider(db)                       # must not raise IntegrityError
    con = sqlite3.connect(db)
    srcs = con.execute("SELECT collection, source_hash FROM kb_sources").fetchall()
    chunks = con.execute("SELECT collection, content FROM kb_chunks").fetchall()
    con.close()
    assert srcs == [(WRITTEN_COLLECTION, WRITTEN_COLLECTION)]
    assert chunks == [(WRITTEN_COLLECTION, "new text")]


@pytest.mark.asyncio
async def test_a_search_hit_resets_the_unread_clock(provider):
    old = datetime.fromtimestamp(time.time() - 200 * 86400, timezone.utc).isoformat()
    await _put(provider, WRITTEN_COLLECTION, "/read.md", "invoice process", created_at=old)
    await _put(provider, WRITTEN_COLLECTION, "/unread.md", "playwright tips", created_at=old)
    cutoff = time.time() - 90 * 86400
    assert set(provider.kb_unread_sources(collection=WRITTEN_COLLECTION,
                                          before_ts=cutoff)) == {
        (USER, "/read.md"), (USER, "/unread.md")}

    await provider.kb_search("invoice", user_id=USER)

    assert provider.kb_unread_sources(collection=WRITTEN_COLLECTION,
                                      before_ts=cutoff) == [(USER, "/unread.md")]


@pytest.mark.asyncio
async def test_retention_ages_out_only_unread_written_documents(provider, monkeypatch):
    import modules.memory.registry as reg
    from agents.task.agent.core.retention import RetentionSweeper

    old = datetime.fromtimestamp(time.time() - 200 * 86400, timezone.utc).isoformat()
    await _put(provider, WRITTEN_COLLECTION, "/stale.md", "playwright tips", created_at=old)
    await _put(provider, WRITTEN_COLLECTION, "/fresh.md", "postgres notes")
    await _put(provider, "default", "/owner.md", "kubernetes runbook", created_at=old)
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "0")
    reg.reset_memory_registry()
    reg.set_external_memory_provider(provider)
    try:
        out = await RetentionSweeper().run_once()
    finally:
        reg.reset_memory_registry()

    assert out["kb_written_removed"] == 1
    left = {s["source_path"] for s in await provider.kb_list_sources(user_id=USER)}
    assert left == {"/fresh.md", "/owner.md"}      # owner-ingested never ages out


@pytest.mark.asyncio
async def test_age_out_can_be_disabled(provider, monkeypatch):
    import modules.memory.registry as reg
    from agents.task.agent.core.retention import RetentionSweeper

    old = datetime.fromtimestamp(time.time() - 200 * 86400, timezone.utc).isoformat()
    await _put(provider, WRITTEN_COLLECTION, "/stale.md", "playwright tips", created_at=old)
    monkeypatch.setenv("KB_WRITTEN_RETENTION_DAYS", "0")
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "0")
    reg.reset_memory_registry()
    reg.set_external_memory_provider(provider)
    try:
        out = await RetentionSweeper().run_once()
    finally:
        reg.reset_memory_registry()
    assert out["kb_written_removed"] == 0


@pytest.mark.skipif(not _vec_available(), reason="apsw / sqlite-vec not installed")
@pytest.mark.asyncio
async def test_vector_search_reads_every_collection(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_REQUIRE_USER_ID", raising=False)

    class _Emb:
        def encode(self, text):
            t = (text or "").lower()
            return [1.0 if "postgres" in t else 0.0, 0.0, 0.0, len(t) % 7 / 100.0]

    p = LocalVectorMemoryProvider(str(tmp_path / "memory.db"), embedding_model=_Emb())
    await _put(p, "default", "/a.md", "postgres tuning guide")
    await _put(p, WRITTEN_COLLECTION, "/b.md", "postgres backup notes")
    out = await p.kb_search("postgres", user_id=USER)
    assert "/a.md" in out and "/b.md" in out


def test_the_retention_ticker_starts_without_the_curator(monkeypatch, tmp_path):
    import asyncio

    import polyrob.app.autonomy_runtime as ar

    class _T:
        started = False

        async def run_forever(self, stop_event=None):
            self.started = True
            await stop_event.wait()

    t = _T()
    monkeypatch.setenv("RETENTION_TICKER_ENABLED", "on")
    monkeypatch.setattr(ar, "_build_retention_ticker", lambda data_dir: t)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: False)
    monkeypatch.setattr(ar, "_cron_enabled", lambda: False)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: False)

    async def go():
        handles = ar.start_autonomy(task_agent=object(), data_dir=str(tmp_path))
        await asyncio.sleep(0.01)
        names = set(handles.loops)
        await handles.stop()
        return names

    names = asyncio.run(go())
    assert "retention" in names and "curator" not in names and t.started
