"""Tests for LocalVectorMemoryProvider (hybrid keyword+vector, local sqlite-vec).

Uses a deterministic fake embedder so the suite needs no model download. A couple of
tests are skipped if apsw / sqlite-vec aren't installed (so the file is importable
everywhere) — but in this repo they ARE deps, so they run.
"""
import os

import pytest

from modules.memory.local_vector_memory_provider import (
    LocalVectorMemoryProvider,
    _vec_available,
)

pytestmark = pytest.mark.asyncio

requires_vec = pytest.mark.skipif(not _vec_available(),
                                  reason="apsw / sqlite-vec not installed")


class FakeEmbedder:
    """Maps text -> a small deterministic vector. Texts sharing a 'topic' keyword get
    near-identical vectors so semantic neighbours are predictable without a real model."""
    DIM = 8
    TOPICS = ["postgres", "kubernetes", "invoice", "playwright"]

    def encode(self, text: str):
        t = (text or "").lower()
        vec = [0.0] * self.DIM
        for i, topic in enumerate(self.TOPICS):
            if topic in t:
                vec[i] = 1.0
        # tiny tie-breaker so non-topic text isn't an all-zero vector
        vec[-1] = (len(t) % 7) / 100.0
        return vec


@pytest.fixture
def db_path(tmp_path):
    # default-on require-user-id is fine; tests pass explicit user ids
    os.environ.pop("MEMORY_REQUIRE_USER_ID", None)
    return str(tmp_path / "memory.db")


@requires_vec
async def test_vec_mode_active_with_embedder(db_path):
    p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    assert p._vec_ok is True
    assert p._dim == FakeEmbedder.DIM
    assert p.name == "local-vector"


async def test_degrades_to_fts_without_embedder(db_path):
    p = LocalVectorMemoryProvider(db_path, embedding_model=None)
    assert p._vec_ok is False
    assert p.name == "local-vector(fts-only)"
    # still works as keyword recall (base behavior)
    await p.sync_turn("tell me about postgres tuning", "use EXPLAIN ANALYZE",
                      session_id="s1", user_id="u1")
    out = await p.search("postgres", user_id="u1", limit=5)
    assert "postgres" in out.lower()


@requires_vec
async def test_semantic_recall_beats_keyword(db_path):
    """A query with NO lexical overlap still recalls the topically-related turn."""
    p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    await p.sync_turn("we migrated the database to postgres", "ok noted",
                      session_id="s1", user_id="u1")
    await p.sync_turn("the kubernetes cluster autoscaled", "ok noted",
                      session_id="s1", user_id="u1")
    # 'postgres' is the only lexical term; FTS5 alone could not match the 2nd turn.
    # Vector recall surfaces the postgres turn even though the query word differs.
    out = await p.prefetch("postgres", session_id="s2", user_id="u1")
    assert "postgres" in out.lower()
    assert "kubernetes" not in out.lower()


@requires_vec
async def test_tenant_isolation_vectors(db_path):
    p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    await p.sync_turn("invoice from acme corp", "filed", session_id="s1", user_id="alice")
    await p.sync_turn("invoice from globex", "filed", session_id="s1", user_id="bob")
    alice = await p.search("invoice", user_id="alice", limit=5)
    bob = await p.search("invoice", user_id="bob", limit=5)
    assert "acme" in alice.lower() and "globex" not in alice.lower()
    assert "globex" in bob.lower() and "acme" not in bob.lower()


@requires_vec
async def test_anon_blocked_writes_and_reads_nothing(db_path):
    os.environ["MEMORY_REQUIRE_USER_ID"] = "true"
    try:
        p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
        await p.sync_turn("postgres secret", "x", session_id="s1", user_id="")
        out = await p.search("postgres", user_id="", limit=5)
        assert out == ""
    finally:
        os.environ.pop("MEMORY_REQUIRE_USER_ID", None)


@requires_vec
async def test_prefetch_empty_query_injects_nothing(db_path):
    p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    await p.sync_turn("postgres notes", "x", session_id="s1", user_id="u1")
    assert await p.prefetch("", session_id="s2", user_id="u1") == ""
    assert await p.prefetch("   ", session_id="s2", user_id="u1") == ""


@requires_vec
async def test_search_browse_on_empty_query(db_path):
    """search() (agent-callable) browses recent rows on empty query (base behavior kept)."""
    p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    await p.sync_turn("first postgres turn", "x", session_id="s1", user_id="u1")
    await p.sync_turn("second kubernetes turn", "x", session_id="s1", user_id="u1")
    out = await p.search("", user_id="u1", limit=5)
    assert "turn" in out.lower()  # browse returns rows


@requires_vec
async def test_failopen_when_vector_query_errors(db_path, monkeypatch):
    p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    await p.sync_turn("postgres tuning tips", "use indexes", session_id="s1", user_id="u1")

    def boom(*a, **k):
        raise RuntimeError("vec exploded")
    monkeypatch.setattr(p, "_vector_contents", boom)
    # keyword half must still come through
    out = await p.search("postgres", user_id="u1", limit=5)
    assert "postgres" in out.lower()


def test_rrf_merge_orders_by_fused_score():
    # content present in both lists (and high in both) should rank first
    a = ["shared", "a-only"]
    b = ["shared", "b-only"]
    merged = LocalVectorMemoryProvider._rrf_merge([a, b], limit=3)
    assert merged[0] == "shared"
    assert set(merged) == {"shared", "a-only", "b-only"}


class _Dim4Embedder(FakeEmbedder):
    DIM = 4

    def encode(self, text: str):
        return [1.0, 0.0, 0.0, (len(text or "") % 7) / 100.0]


@requires_vec
async def test_embedding_dim_change_rebuilds_vector_store(db_path):
    """A model swap (dim change) must rebuild mem_vec, not silently fail every write."""
    # First provider: 8-dim embedder, write + recall a vector row.
    p1 = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    assert p1._vec_ok
    await p1.sync_turn("postgres tuning notes", "use indexes",
                       session_id="s1", user_id="u1")
    # Second provider on the SAME db with a 4-dim embedder: must not crash, must
    # rebuild the store and accept new writes at the new width.
    p2 = LocalVectorMemoryProvider(db_path, embedding_model=_Dim4Embedder())
    assert p2._vec_ok  # vector half still healthy after rebuild
    await p2.sync_turn("kubernetes scaling", "add nodes",
                       session_id="s2", user_id="u1")
    out = await p2.search("kubernetes", user_id="u1", limit=5)
    assert "kubernetes" in out.lower()


@requires_vec
async def test_prefetch_runs_vector_search_once(db_path, monkeypatch):
    """B10/B19: prefetch must NOT run the vector KNN twice. It previously called
    super().prefetch() (which dispatches to this class's hybrid search override) and
    then recomputed the vector half — double embed + KNN per step."""
    p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    await p.sync_turn("postgres tuning notes for the db", "x", session_id="s1", user_id="u1")

    calls = {"n": 0}
    orig = p._vector_contents

    def spy(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(p, "_vector_contents", spy)
    await p.prefetch("postgres", session_id="s2", user_id="u1")
    assert calls["n"] == 1
