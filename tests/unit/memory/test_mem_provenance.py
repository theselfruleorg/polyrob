"""B2/D1 — provenance sidecar for the cross-session `memories` store.

`memories` is FTS5 and can't be ALTERed, so provenance lives in a sidecar table
`mem_provenance` keyed by the FTS rowid (NOT `mem_meta` — that name is taken by
the local_vector provider's vector sidecar). Every new sync_turn write stamps
(ts, kind, content_hash); recall renders a `[YYYY-MM-DD]` prefix for stamped
rows and stays bare for legacy rows. Exact duplicates collapse at write: the
existing row's ts is refreshed instead of inserting a twin (GEM revision
semantics, cheapest form).
"""
from __future__ import annotations

import time

import pytest

from core.sqlite_util import execute_retry
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

USER = "user_prov"


@pytest.fixture()
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "false")
    monkeypatch.delenv("MEMORY_ROW_MAX_CHARS", raising=False)
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


@pytest.mark.asyncio
async def test_sync_turn_stamps_provenance(provider):
    await provider.sync_turn("what is X", "X is 42", session_id="s1", user_id=USER)
    rows = execute_retry(
        provider.db_path,
        "SELECT p.ts, p.kind, p.content_hash, p.user_id FROM mem_provenance p",
        fetch="all")
    assert len(rows) == 1
    assert rows[0]["kind"] == "finding"
    assert rows[0]["user_id"] == USER
    assert abs(rows[0]["ts"] - time.time()) < 60
    assert rows[0]["content_hash"]


@pytest.mark.asyncio
async def test_recall_lines_are_dated(provider):
    await provider.sync_turn("what is X", "X is 42", session_id="s1", user_id=USER)
    out = await provider.search("42", user_id=USER)
    today = time.strftime("%Y-%m-%d")
    assert out.startswith(f"- [{today}] ")
    out_pf = await provider.prefetch("what happened", session_id="other", user_id=USER)
    assert f"[{today}]" in out_pf


@pytest.mark.asyncio
async def test_legacy_rows_render_bare(provider):
    # A pre-sidecar row: written straight into the FTS table, no provenance.
    execute_retry(
        provider.db_path,
        "INSERT INTO memories (user_id, session_id, content) VALUES (?,?,?)",
        (USER, "old", "legacy fact about Y"))
    out = await provider.search("legacy", user_id=USER)
    assert out == "- legacy fact about Y"


@pytest.mark.asyncio
async def test_exact_duplicate_collapses(provider):
    await provider.sync_turn("q", "the same finding", session_id="s1", user_id=USER)
    first_ts = execute_retry(provider.db_path,
                             "SELECT ts FROM mem_provenance", fetch="all")[0]["ts"]
    # Force a visible refresh delta.
    execute_retry(provider.db_path,
                  "UPDATE mem_provenance SET ts = ts - 1000")
    await provider.sync_turn("q", "the same finding", session_id="s2", user_id=USER)
    mem_rows = execute_retry(provider.db_path,
                             "SELECT content FROM memories", fetch="all")
    prov_rows = execute_retry(provider.db_path,
                              "SELECT ts FROM mem_provenance", fetch="all")
    assert len(mem_rows) == 1
    assert len(prov_rows) == 1
    assert abs(prov_rows[0]["ts"] - first_ts) < 60  # refreshed back to ~now


@pytest.mark.asyncio
async def test_duplicate_scoped_per_tenant(provider):
    await provider.sync_turn("q", "shared text", session_id="s1", user_id=USER)
    await provider.sync_turn("q", "shared text", session_id="s1", user_id="user_other")
    mem_rows = execute_retry(provider.db_path,
                             "SELECT user_id FROM memories", fetch="all")
    assert len(mem_rows) == 2


@pytest.mark.asyncio
async def test_keyword_contents_contract_unchanged(provider):
    """Subclass RRF contract: _keyword_contents still returns bare content strings."""
    await provider.sync_turn("q", "alpha beta gamma", session_id="s1", user_id=USER)
    contents = provider._keyword_contents("alpha", norm_user=USER, limit=5)
    assert contents == ["User: q\nAssistant: alpha beta gamma"]


@pytest.mark.asyncio
async def test_vector_half_skipped_on_dup_collapse(tmp_path, monkeypatch):
    """A collapsed duplicate must NOT grow the vector sidecar: the hybrid
    provider's sync_turn skips embedding when the keyword half reported a dup."""
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "false")
    from modules.memory.local_vector_memory_provider import LocalVectorMemoryProvider

    class _Embedder:
        def encode(self, text, **kw):
            return [0.0, 1.0]

    p = LocalVectorMemoryProvider(str(tmp_path / "memory.db"),
                                  embedding_model=_Embedder())
    p._vec_ok = True  # pretend apsw/sqlite-vec are present
    writes = []
    monkeypatch.setattr(p, "_vec_write",
                        lambda *a, **k: writes.append(a))
    monkeypatch.setattr(p, "_embed",
                        _async_return([0.0, 1.0]))
    await p.sync_turn("q", "the same finding", session_id="s1", user_id=USER)
    await p.sync_turn("q", "the same finding", session_id="s2", user_id=USER)
    assert len(writes) == 1


def _async_return(value):
    async def _inner(*a, **k):
        return value
    return _inner


@pytest.mark.asyncio
async def test_prune_batches_past_param_limit(provider, monkeypatch):
    """Retention must chunk its DELETE ... IN lists (a big backlog would exceed
    SQLite's bound-parameter limit and the error is swallowed fail-open)."""
    monkeypatch.setattr(SqliteMemoryProvider, "_PRUNE_BATCH", 2)
    for i in range(5):
        await provider.sync_turn("q", f"old finding {i}", session_id="s1", user_id=USER)
    execute_retry(provider.db_path, "UPDATE mem_provenance SET ts = ts - 10000000")
    removed = provider.prune_memories(older_than_ts=int(time.time()) - 1000)
    assert removed == 5
    rows = execute_retry(provider.db_path,
                         "SELECT COUNT(*) AS n FROM memories", fetch="all")
    assert rows[0]["n"] == 0
