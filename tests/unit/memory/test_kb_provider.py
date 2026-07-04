"""TDD tests for KB (knowledge-base) storage methods on memory providers.

Tests the new kb_* methods added to SqliteMemoryProvider (FTS-only) and
LocalVectorMemoryProvider (hybrid). Uses real tmp SQLite files; only the embedder
is stubbed. Vector tests are skipped gracefully when apsw/sqlite-vec is not loadable.
"""
import os

import pytest

from modules.memory.local_vector_memory_provider import (
    LocalVectorMemoryProvider,
    _vec_available,
)
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

pytestmark = pytest.mark.asyncio

requires_vec = pytest.mark.skipif(
    not _vec_available(), reason="apsw / sqlite-vec not installed"
)


# ---------------------------------------------------------------------------
# Fake embedder (deterministic, no model download)
# ---------------------------------------------------------------------------

class FakeEmbedder:
    """Maps text to a small deterministic vector by topic keywords."""
    DIM = 8
    TOPICS = ["postgres", "kubernetes", "invoice", "playwright"]

    def encode(self, text: str):
        t = (text or "").lower()
        vec = [0.0] * self.DIM
        for i, topic in enumerate(self.TOPICS):
            if topic in t:
                vec[i] = 1.0
        vec[-1] = (len(t) % 7) / 100.0
        return vec


class _Dim4Embedder(FakeEmbedder):
    """Smaller embedder for dim-change rebuild test."""
    DIM = 4

    def encode(self, text: str):
        return [1.0, 0.0, 0.0, (len(text or "") % 7) / 100.0]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    os.environ.pop("MEMORY_REQUIRE_USER_ID", None)
    return str(tmp_path / "memory.db")


@pytest.fixture
def provider(db_path):
    return SqliteMemoryProvider(db_path)


# ---------------------------------------------------------------------------
# FTS-only path (SqliteMemoryProvider)
# ---------------------------------------------------------------------------

async def test_kb_ingest_and_search_fts(provider):
    """Ingest 3 chunks, kb_search returns provenance-tagged hits."""
    await provider.kb_ingest_chunk(
        user_id="u1", collection="docs",
        source_path="doc1.pdf", source_hash="hash1",
        chunk_idx=0, content="postgres index tuning guide",
        mime="text/plain", created_at="2024-01-01",
    )
    await provider.kb_ingest_chunk(
        user_id="u1", collection="docs",
        source_path="doc1.pdf", source_hash="hash1",
        chunk_idx=1, content="use EXPLAIN ANALYZE to find slow queries",
        mime="text/plain", created_at="2024-01-01",
    )
    await provider.kb_ingest_chunk(
        user_id="u1", collection="docs",
        source_path="doc2.pdf", source_hash="hash2",
        chunk_idx=0, content="kubernetes autoscaling hpa vpa",
        mime="text/plain", created_at="2024-01-02",
    )

    result = await provider.kb_search("postgres", user_id="u1", collection="docs")
    assert result  # non-empty
    assert "postgres" in result.lower() or "EXPLAIN" in result
    # provenance tags present
    assert "[" in result and "#" in result


async def test_kb_search_provenance_format(provider):
    """Each result line contains [source_path #chunk_idx] provenance tag."""
    await provider.kb_ingest_chunk(
        user_id="u1", collection="col",
        source_path="guide.md", source_hash="h1",
        chunk_idx=3, content="invoice processing workflow",
        mime="text/plain", created_at="2024-01-01",
    )
    result = await provider.kb_search("invoice", user_id="u1", collection="col")
    assert "guide.md" in result
    assert "#3" in result


async def test_kb_cross_tenant_isolation(provider):
    """Ingested by u1, searching as u2 returns empty string."""
    await provider.kb_ingest_chunk(
        user_id="u1", collection="private",
        source_path="secret.txt", source_hash="h1",
        chunk_idx=0, content="sensitive postgres credentials",
        mime="text/plain", created_at="2024-01-01",
    )
    result_u2 = await provider.kb_search("postgres", user_id="u2", collection="private")
    assert result_u2 == ""


async def test_kb_cross_collection_isolation(provider):
    """Ingested in collection 'a', searching in collection 'b' returns empty."""
    await provider.kb_ingest_chunk(
        user_id="u1", collection="colA",
        source_path="doc.txt", source_hash="h1",
        chunk_idx=0, content="postgres replication setup",
        mime="text/plain", created_at="2024-01-01",
    )
    result = await provider.kb_search("postgres", user_id="u1", collection="colB")
    assert result == ""


async def test_kb_source_hash_round_trip(provider):
    """kb_source_hash returns the stored hash, differs after re-ingest with new hash."""
    await provider.kb_ingest_chunk(
        user_id="u1", collection="col",
        source_path="file.pdf", source_hash="hash_v1",
        chunk_idx=0, content="initial content",
        mime="text/plain", created_at="2024-01-01",
    )
    h = provider.kb_source_hash(user_id="u1", collection="col", source_path="file.pdf")
    assert h == "hash_v1"

    # Re-ingest with new hash
    await provider.kb_ingest_chunk(
        user_id="u1", collection="col",
        source_path="file.pdf", source_hash="hash_v2",
        chunk_idx=0, content="updated content",
        mime="text/plain", created_at="2024-01-02",
    )
    h2 = provider.kb_source_hash(user_id="u1", collection="col", source_path="file.pdf")
    assert h2 == "hash_v2"


async def test_kb_source_hash_missing(provider):
    """Returns None when the source hasn't been ingested."""
    h = provider.kb_source_hash(user_id="u1", collection="col", source_path="missing.pdf")
    assert h is None


async def test_kb_remove_scoped(provider):
    """kb_remove(source=...) removes only that source's chunks, leaves others."""
    for i in range(3):
        await provider.kb_ingest_chunk(
            user_id="u1", collection="col",
            source_path="doc_a.pdf", source_hash="ha",
            chunk_idx=i, content=f"doc_a chunk {i} postgres",
            mime="text/plain", created_at="2024-01-01",
        )
    for i in range(2):
        await provider.kb_ingest_chunk(
            user_id="u1", collection="col",
            source_path="doc_b.pdf", source_hash="hb",
            chunk_idx=i, content=f"doc_b chunk {i} kubernetes",
            mime="text/plain", created_at="2024-01-01",
        )

    removed = await provider.kb_remove(user_id="u1", collection="col", source="doc_a.pdf")
    assert removed == 3

    # doc_b still searchable
    result = await provider.kb_search("kubernetes", user_id="u1", collection="col")
    assert "doc_b" in result

    # doc_a gone
    result_a = await provider.kb_search("doc_a", user_id="u1", collection="col")
    # either empty or no doc_a content
    assert "doc_a chunk" not in result_a


async def test_kb_remove_collection(provider):
    """kb_remove without source removes entire collection for this tenant."""
    for i in range(2):
        await provider.kb_ingest_chunk(
            user_id="u1", collection="trash",
            source_path=f"f{i}.txt", source_hash=f"h{i}",
            chunk_idx=0, content=f"content {i} postgres",
            mime="text/plain", created_at="2024-01-01",
        )
    removed = await provider.kb_remove(user_id="u1", collection="trash")
    assert removed == 2
    result = await provider.kb_search("postgres", user_id="u1", collection="trash")
    assert result == ""


async def test_kb_remove_other_tenant_unaffected(provider):
    """Removing a collection for u1 does not touch u2's data."""
    await provider.kb_ingest_chunk(
        user_id="u1", collection="col",
        source_path="f.txt", source_hash="h1",
        chunk_idx=0, content="postgres notes",
        mime="text/plain", created_at="2024-01-01",
    )
    await provider.kb_ingest_chunk(
        user_id="u2", collection="col",
        source_path="f.txt", source_hash="h1",
        chunk_idx=0, content="postgres notes",
        mime="text/plain", created_at="2024-01-01",
    )
    await provider.kb_remove(user_id="u1", collection="col")
    result_u2 = await provider.kb_search("postgres", user_id="u2", collection="col")
    assert "postgres" in result_u2.lower()


async def test_kb_list_sources_all_collections(provider):
    """kb_list_sources(collection=None) returns sources across all collections."""
    await provider.kb_ingest_chunk(
        user_id="u1", collection="col1",
        source_path="a.pdf", source_hash="h1",
        chunk_idx=0, content="content",
        mime="text/plain", created_at="2024-01-01",
    )
    await provider.kb_ingest_chunk(
        user_id="u1", collection="col2",
        source_path="b.pdf", source_hash="h2",
        chunk_idx=0, content="content",
        mime="text/plain", created_at="2024-01-01",
    )
    sources = await provider.kb_list_sources(user_id="u1")
    paths = [s["source_path"] for s in sources]
    assert "a.pdf" in paths
    assert "b.pdf" in paths


async def test_kb_list_sources_filtered_by_collection(provider):
    """kb_list_sources(collection='col1') returns only that collection's sources."""
    await provider.kb_ingest_chunk(
        user_id="u1", collection="col1",
        source_path="a.pdf", source_hash="h1",
        chunk_idx=0, content="content",
        mime="text/plain", created_at="2024-01-01",
    )
    await provider.kb_ingest_chunk(
        user_id="u1", collection="col2",
        source_path="b.pdf", source_hash="h2",
        chunk_idx=0, content="content",
        mime="text/plain", created_at="2024-01-01",
    )
    sources = await provider.kb_list_sources(user_id="u1", collection="col1")
    paths = [s["source_path"] for s in sources]
    assert "a.pdf" in paths
    assert "b.pdf" not in paths


async def test_kb_anon_ingest_noop_under_require_user_id(db_path, monkeypatch):
    """Anon ingest is a no-op and search returns '' under MEMORY_REQUIRE_USER_ID=true."""
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    p = SqliteMemoryProvider(db_path)
    await p.kb_ingest_chunk(
        user_id="", collection="col",
        source_path="f.txt", source_hash="h1",
        chunk_idx=0, content="secret postgres content",
        mime="text/plain", created_at="2024-01-01",
    )
    result = await p.kb_search("postgres", user_id="", collection="col")
    assert result == ""


async def test_kb_search_empty_query_returns_recent(provider):
    """Empty query falls back to recent browse (rowid DESC)."""
    await provider.kb_ingest_chunk(
        user_id="u1", collection="col",
        source_path="doc.txt", source_hash="h1",
        chunk_idx=0, content="some browsable content here",
        mime="text/plain", created_at="2024-01-01",
    )
    result = await provider.kb_search("", user_id="u1", collection="col")
    assert "browsable content" in result


async def test_kb_search_meaningless_query_no_recent_browse(provider):
    """A NON-empty query that yields no usable FTS terms must NOT fall back to recent
    browse (that returns unrelated chunks). Empty query still browses (separate test)."""
    await provider.kb_ingest_chunk(
        user_id="u1", collection="col",
        source_path="d.txt", source_hash="h",
        chunk_idx=0, content="postgres tuning notes",
        mime="text/plain", created_at="2024-01-01",
    )
    result = await provider.kb_search("?? !!", user_id="u1", collection="col")
    assert result == ""


async def test_kb_ingest_chunk_returns_true_on_success(provider):
    """kb_ingest_chunk signals success with True so callers can detect partial failure."""
    ok = await provider.kb_ingest_chunk(
        user_id="u1", collection="c", source_path="f", source_hash="h",
        chunk_idx=0, content="postgres", mime="text/plain", created_at="x",
    )
    assert ok is True


async def test_kb_ingest_chunk_returns_false_on_anon_block(db_path, monkeypatch):
    """Anon-blocked ingest returns False (not None) so it's not mistaken for success."""
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    p = SqliteMemoryProvider(db_path)
    ok = await p.kb_ingest_chunk(
        user_id="", collection="c", source_path="f", source_hash="h",
        chunk_idx=0, content="x", mime="text/plain", created_at="x",
    )
    assert ok is False


async def test_registry_kb_ingest_chunk_returns_false_no_provider():
    """Registry router returns False when no provider is registered."""
    from modules.memory.registry import kb_ingest_chunk, reset_memory_registry
    reset_memory_registry()
    ok = await kb_ingest_chunk(
        user_id="u1", collection="c", source_path="f", source_hash="h",
        chunk_idx=0, content="x",
    )
    assert ok is False
    reset_memory_registry()


async def test_kb_keyword_contents_sanitizer(provider):
    """FTS sanitizer correctly strips short tokens, returns hits for valid terms.
    kb_keyword_contents returns list of (content, source_path, chunk_idx) tuples.
    """
    await provider.kb_ingest_chunk(
        user_id="u1", collection="docs",
        source_path="report.pdf", source_hash="h1",
        chunk_idx=0, content="quarterly invoice reconciliation report",
        mime="text/plain", created_at="2024-01-01",
    )
    rows = provider.kb_keyword_contents(
        "invoice reconciliation", user_id="u1", collection="docs", limit=5
    )
    # rows is a list of (content, source_path, chunk_idx) tuples
    assert rows
    assert any("invoice" in content.lower() for content, _, _ in rows)


# ---------------------------------------------------------------------------
# Vector path (LocalVectorMemoryProvider)
# ---------------------------------------------------------------------------

@requires_vec
async def test_kb_vector_rrf_engages(db_path):
    """With a real fake embedder, RRF merges vector candidates into kb_search."""
    p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    assert p._vec_ok

    await p.kb_ingest_chunk(
        user_id="u1", collection="docs",
        source_path="pg_guide.pdf", source_hash="h1",
        chunk_idx=0, content="postgres performance tuning index optimization",
        mime="text/plain", created_at="2024-01-01",
    )
    await p.kb_ingest_chunk(
        user_id="u1", collection="docs",
        source_path="k8s_guide.pdf", source_hash="h2",
        chunk_idx=0, content="kubernetes cluster autoscaling pod management",
        mime="text/plain", created_at="2024-01-01",
    )

    result = await p.kb_search("postgres", user_id="u1", collection="docs")
    assert result  # non-empty
    # The postgres-topic chunk should appear
    assert "postgres" in result.lower() or "pg_guide" in result


@requires_vec
async def test_kb_vector_tenant_isolation(db_path):
    """Vector recall is tenant-scoped: u2 gets nothing from u1's ingested content."""
    p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    await p.kb_ingest_chunk(
        user_id="u1", collection="private",
        source_path="secret.pdf", source_hash="h1",
        chunk_idx=0, content="postgres credentials and connection strings",
        mime="text/plain", created_at="2024-01-01",
    )
    result = await p.kb_search("postgres", user_id="u2", collection="private")
    assert result == ""


@requires_vec
async def test_kb_dim_change_rebuilds_vec_keeps_chunks(db_path):
    """A dim change (new embedder) rebuilds kb_vec/kb_meta but kb_chunks FTS rows survive."""
    # First provider: 8-dim embedder
    p1 = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    assert p1._vec_ok
    await p1.kb_ingest_chunk(
        user_id="u1", collection="docs",
        source_path="doc.pdf", source_hash="h1",
        chunk_idx=0, content="postgres tuning notes index",
        mime="text/plain", created_at="2024-01-01",
    )

    # Second provider on the SAME db with a 4-dim embedder: must not crash,
    # must rebuild kb_vec/kb_meta while kb_chunks FTS rows survive.
    p2 = LocalVectorMemoryProvider(db_path, embedding_model=_Dim4Embedder())
    assert p2._vec_ok  # vector half healthy after rebuild

    # FTS keyword recall still works (kb_chunks not dropped)
    result_fts = await p2.kb_search("postgres", user_id="u1", collection="docs")
    assert "postgres" in result_fts.lower()

    # New ingest + search works at new dimension
    await p2.kb_ingest_chunk(
        user_id="u1", collection="docs",
        source_path="new_doc.pdf", source_hash="h2",
        chunk_idx=0, content="kubernetes autoscaling config",
        mime="text/plain", created_at="2024-01-02",
    )
    result2 = await p2.kb_search("kubernetes", user_id="u1", collection="docs")
    assert "kubernetes" in result2.lower()


@requires_vec
async def test_kb_remove_also_clears_vector(db_path):
    """kb_remove deletes kb_meta/kb_vec rows in addition to kb_chunks."""
    p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())
    await p.kb_ingest_chunk(
        user_id="u1", collection="col",
        source_path="doc.pdf", source_hash="h1",
        chunk_idx=0, content="invoice processing workflow details",
        mime="text/plain", created_at="2024-01-01",
    )
    removed = await p.kb_remove(user_id="u1", collection="col", source="doc.pdf")
    assert removed >= 1
    result = await p.kb_search("invoice", user_id="u1", collection="col")
    assert result == ""


@requires_vec
async def test_kb_vector_failopen_on_embed_error(db_path, monkeypatch):
    """Vector failure during ingest must not break FTS write (fail-open)."""
    p = LocalVectorMemoryProvider(db_path, embedding_model=FakeEmbedder())

    async def boom(*a, **kw):
        raise RuntimeError("embedder exploded")

    monkeypatch.setattr(p, "_embed", boom)

    # Should not raise — FTS write succeeds, vector silently skipped
    await p.kb_ingest_chunk(
        user_id="u1", collection="col",
        source_path="doc.pdf", source_hash="h1",
        chunk_idx=0, content="invoice workflow postgres",
        mime="text/plain", created_at="2024-01-01",
    )

    # FTS path still works
    result = await p.kb_search("invoice", user_id="u1", collection="col")
    assert "invoice" in result.lower()


# ---------------------------------------------------------------------------
# Registry routers
# ---------------------------------------------------------------------------

async def test_registry_kb_search_no_provider():
    """kb_search via registry returns '' when no provider registered."""
    from modules.memory.registry import kb_search, reset_memory_registry
    reset_memory_registry()
    result = await kb_search("test", user_id="u1", collection="col")
    assert result == ""
    reset_memory_registry()


async def test_registry_kb_list_sources_no_provider():
    from modules.memory.registry import kb_list_sources, reset_memory_registry
    reset_memory_registry()
    result = await kb_list_sources(user_id="u1")
    assert result == []
    reset_memory_registry()


async def test_registry_kb_remove_no_provider():
    from modules.memory.registry import kb_remove, reset_memory_registry
    reset_memory_registry()
    result = await kb_remove(user_id="u1", collection="col")
    assert result == 0
    reset_memory_registry()


async def test_registry_kb_source_hash_no_provider():
    from modules.memory.registry import kb_source_hash, reset_memory_registry
    reset_memory_registry()
    result = await kb_source_hash(user_id="u1", collection="col", source_path="f.pdf")
    assert result is None
    reset_memory_registry()


async def test_registry_kb_routes_to_active_provider(db_path):
    """Registry routes kb_search to the active external provider."""
    from modules.memory.registry import (
        kb_ingest_chunk, kb_search, reset_memory_registry,
        set_external_memory_provider,
    )
    reset_memory_registry()
    p = SqliteMemoryProvider(db_path)
    set_external_memory_provider(p)

    await kb_ingest_chunk(
        user_id="u1", collection="col",
        source_path="doc.txt", source_hash="h1",
        chunk_idx=0, content="postgres schema migration",
        mime="text/plain", created_at="2024-01-01",
    )
    result = await kb_search("postgres", user_id="u1", collection="col")
    assert "postgres" in result.lower()
    reset_memory_registry()
