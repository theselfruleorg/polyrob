"""Local hybrid (keyword + vector) cross-session memory backend.

Extends :class:`SqliteMemoryProvider` (FTS5 keyword recall) with a **local** vector
layer so semantic cross-session recall reaches the agent step loop — with **zero cloud
dependency** (replaces the legacy Pinecone-backed RAG). Everything lives in one
``memory.db`` file:

    memories        FTS5(user_id, session_id, content)   <- inherited (keyword)
    curated_memory  (memory tool)                         <- inherited
    mem_meta        (rowid, user_id, session_id, content) <- vector sidecar
    mem_vec         vec0(user_id PARTITION KEY, embedding) <- sqlite-vec (semantic)

The keyword half uses the stdlib ``sqlite3`` path (``core/sqlite_util``) exactly as
today; the vector half uses **apsw** (cross-platform wheels, supports
``loadextension`` — stdlib ``sqlite3`` is frequently built without it) to load the
``sqlite-vec`` extension. Both connect to the same file; each touches only its own
tables, so they coexist safely (verified).

Embeddings reuse the container's already-loaded local sentence-transformers model
(no second copy, no API cost). Recall merges keyword + vector candidates via
**Reciprocal Rank Fusion**.

Fail-open by construction: if apsw / sqlite-vec / the embedding model is unavailable
or errors, the provider silently degrades to **FTS5-keyword-only** (identical to the
base class) — it never crashes the agent loop. Opt-in via ``MEMORY_BACKEND=local_vector``
(default stays ``sqlite``).
"""
import asyncio
import logging
import os
import re
from typing import List, Optional

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

logger = logging.getLogger(__name__)

# RRF constant — standard value; dampens the influence of low ranks.
_RRF_K = 60


# KB vector KNN over-fetch: `k` limits the KNN BEFORE the per-collection filter, so
# fetch a multiple of the requested limit (capped) and slice after filtering, to keep
# recall for the queried collection when another collection dominates the top-k.
_KB_VEC_OVERFETCH = 8
_KB_VEC_OVERFETCH_CAP = 64


def _max_distance() -> float:
    """Cosine-distance cutoff for vector neighbours (= 1 - cosine_similarity, range
    [0,2]). Drops topically-unrelated rows that KNN would otherwise return when the
    store is small. Mirrors the legacy RAG min_score; tunable via env."""
    try:
        return float(os.getenv("MEMORY_VECTOR_MAX_DISTANCE", "0.6"))
    except ValueError:
        return 0.6


def _vec_available() -> bool:
    """True if apsw + sqlite-vec import (cheap; does not open a connection)."""
    try:
        import apsw  # noqa: F401
        import sqlite_vec  # noqa: F401
        return True
    except Exception:
        return False


def vec_connect(db_path: str, *, busy_timeout_ms: int = 3000):
    """Open an apsw connection to *db_path* with WAL + the sqlite-vec extension loaded.

    Kept module-local (not in ``core/sqlite_util``) so ``core`` stays free of the
    optional apsw dependency. Caller owns the connection lifecycle (use + close).
    """
    import apsw
    import sqlite_vec

    con = apsw.Connection(db_path)
    con.setbusytimeout(busy_timeout_ms)
    con.enableloadextension(True)
    try:
        sqlite_vec.load(con)
    finally:
        con.enableloadextension(False)
    cur = con.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
    except apsw.SQLError:
        cur.execute("PRAGMA journal_mode=DELETE")
    return con


class LocalVectorMemoryProvider(SqliteMemoryProvider):
    """Hybrid keyword+vector memory provider. One external provider, one DB file."""

    def __init__(self, db_path: str, *, embedding_model=None, top_k: int = 5):
        # Set vector attributes BEFORE super().__init__ — it calls self._init_schema(),
        # which we override and which needs _vec_ok/_dim/_embedder.
        self._embedder = embedding_model
        self._dim: Optional[int] = None
        self._vec_ok = embedding_model is not None and _vec_available()
        if self._vec_ok:
            try:
                self._dim = len(self._embed_sync("dimension probe"))
            except Exception as e:  # bad/incompatible embedder -> degrade
                logger.warning("local-vector: embedder probe failed, FTS5-only: %s", e)
                self._vec_ok = False
        super().__init__(db_path, top_k=top_k)

    @property
    def name(self) -> str:
        return "local-vector" if self._vec_ok else "local-vector(fts-only)"

    # ---- schema -------------------------------------------------------------
    def _init_schema(self) -> None:
        super()._init_schema()  # FTS5 `memories` + `curated_memory` + `kb_chunks`/`kb_sources` (stdlib path)
        if not self._vec_ok:
            return
        try:
            con = vec_connect(self.db_path)
            try:
                cur = con.cursor()
                # Persist the embedding dimension so a model swap (e.g. 384->768) is
                # detected. Without this, mem_vec kept its old `float[N]` width (the
                # CREATE ... IF NOT EXISTS was a no-op), and EVERY subsequent vector
                # write raised a width mismatch — swallowed fail-open — silently
                # killing the semantic half while the provider reported healthy.
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS mem_vec_config (k TEXT PRIMARY KEY, v TEXT)"
                )
                row = cur.execute(
                    "SELECT v FROM mem_vec_config WHERE k = 'dim'"
                ).fetchone()
                stored_dim = int(row[0]) if row and row[0] is not None else None
                if stored_dim is not None and stored_dim != self._dim:
                    logger.warning(
                        "local-vector: embedding dim changed %s->%s; rebuilding vector "
                        "store (old vectors discarded, FTS5 keyword rows retained)",
                        stored_dim, self._dim,
                    )
                    with con:
                        cur.execute("DROP TABLE IF EXISTS mem_vec")
                        cur.execute("DROP TABLE IF EXISTS mem_meta")
                cur.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS mem_vec USING vec0("
                    f"user_id text partition key, embedding float[{self._dim}] distance_metric=cosine)"
                )
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS mem_meta ("
                    "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "user_id TEXT, session_id TEXT, content TEXT)"
                )
                with con:
                    cur.execute(
                        "INSERT INTO mem_vec_config (k, v) VALUES ('dim', ?) "
                        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                        (str(self._dim),),
                    )
                # KB vector tables — separate from mem_vec/mem_meta.
                # kb_vec_config stores the KB dim; a change drops+recreates kb_vec/kb_meta
                # while kb_chunks FTS rows survive (same pattern as the mem_* dim guard).
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS kb_vec_config (k TEXT PRIMARY KEY, v TEXT)"
                )
                kb_dim_row = cur.execute(
                    "SELECT v FROM kb_vec_config WHERE k = 'dim'"
                ).fetchone()
                stored_kb_dim = int(kb_dim_row[0]) if kb_dim_row and kb_dim_row[0] is not None else None
                if stored_kb_dim is not None and stored_kb_dim != self._dim:
                    logger.warning(
                        "local-vector: KB embedding dim changed %s->%s; rebuilding KB "
                        "vector store (kb_chunks FTS rows retained)",
                        stored_kb_dim, self._dim,
                    )
                    with con:
                        cur.execute("DROP TABLE IF EXISTS kb_vec")
                        cur.execute("DROP TABLE IF EXISTS kb_meta")
                cur.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS kb_vec USING vec0("
                    f"user_id text partition key, embedding float[{self._dim}] distance_metric=cosine)"
                )
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS kb_meta ("
                    "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "user_id TEXT, collection TEXT, source_path TEXT, chunk_idx TEXT, content TEXT)"
                )
                with con:
                    cur.execute(
                        "INSERT INTO kb_vec_config (k, v) VALUES ('dim', ?) "
                        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                        (str(self._dim),),
                    )
            finally:
                con.close()
        except Exception as e:  # extension load / DDL failure -> degrade to FTS5-only
            logger.warning("local-vector: vector schema init failed, FTS5-only: %s", e)
            self._vec_ok = False

    # ---- embedding ----------------------------------------------------------
    def _embed_sync(self, text: str) -> List[float]:
        """Encode *text* -> list[float]. Normalized for cosine. Runs on caller thread;
        callers route this through an executor to keep the loop responsive."""
        # show_progress_bar=False: avoid tqdm "Batches:" bars leaking into the CLI UI.
        try:
            vec = self._embedder.encode(text, show_progress_bar=False)
        except TypeError:
            vec = self._embedder.encode(text)
        tolist = getattr(vec, "tolist", None)
        return tolist() if callable(tolist) else list(vec)

    async def _embed(self, text: str) -> List[float]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._embed_sync, text)

    # ---- write --------------------------------------------------------------
    async def sync_turn(self, user_content: str, assistant_content: str, *,
                        session_id: str, user_id=None) -> None:
        # Keyword half (inherited, unchanged) writes the FTS5 row + applies anon-block.
        await super().sync_turn(user_content, assistant_content,
                                session_id=session_id, user_id=user_id)
        if not self._vec_ok or self._anon_blocked(user_id):
            return
        # Phase 1.1: SAME composition as the keyword half (base class) so the embedded
        # vector content matches the FTS content -> RRF dedup-by-content stays consistent.
        content = self._compose_stored_content(user_content, assistant_content)
        if not content:
            return
        try:
            emb = await self._embed(content)
            await asyncio.get_event_loop().run_in_executor(
                None, self._vec_write, self._norm_user(user_id), session_id, content, emb)
        except Exception as e:  # fail-open: keyword row already persisted
            logger.debug("local-vector: vector write skipped: %s", e)

    def _vec_write(self, norm_user: str, session_id: str, content: str,
                   emb: List[float]) -> None:
        import sqlite_vec
        con = vec_connect(self.db_path)
        try:
            with con:
                cur = con.cursor()
                cur.execute(
                    "INSERT INTO mem_meta (user_id, session_id, content) VALUES (?,?,?)",
                    (norm_user, session_id, content))
                rowid = con.last_insert_rowid()
                cur.execute(
                    "INSERT INTO mem_vec (rowid, user_id, embedding) VALUES (?,?,?)",
                    (rowid, norm_user, sqlite_vec.serialize_float32(emb)))
        finally:
            con.close()

    # ---- read ---------------------------------------------------------------
    def _vector_contents(self, query: str, norm_user: str, limit: int) -> List[str]:
        """Tenant-scoped semantic KNN -> ranked list of content strings (best first)."""
        import sqlite_vec
        emb = self._embed_sync(query)
        con = vec_connect(self.db_path)
        try:
            rows = con.cursor().execute(
                "SELECT m.content, v.distance FROM mem_vec v "
                "JOIN mem_meta m ON m.rowid = v.rowid "
                "WHERE v.user_id = ? AND v.embedding MATCH ? AND k = ? "
                "ORDER BY v.distance",
                (norm_user, sqlite_vec.serialize_float32(emb), limit)).fetchall()
            cutoff = _max_distance()
            # Require a numeric distance within the cutoff. A None distance must NOT
            # pass (the old `r[1] is None or ...` admitted unranked rows, defeating
            # the relevance filter).
            return [r[0] for r in rows if r[1] is not None and r[1] <= cutoff]
        finally:
            con.close()

    @staticmethod
    def _rrf_merge(ranked_lists: List[List[str]], limit: int) -> List[str]:
        """Reciprocal Rank Fusion over several ranked lists of content strings."""
        scores: dict = {}
        for lst in ranked_lists:
            for rank, content in enumerate(lst):
                scores[content] = scores.get(content, 0.0) + 1.0 / (_RRF_K + rank + 1)
        ranked = sorted(scores, key=lambda c: scores[c], reverse=True)
        return ranked[:limit]

    async def search(self, query: str, *, user_id=None, session_id: str = None,
                     limit: int = 5, sort: str = None) -> str:
        if self._anon_blocked(user_id):
            return ""
        limit = self._clamp_limit(limit, self.top_k)
        norm = self._norm_user(user_id)
        kw_list = self._keyword_contents(query, norm_user=norm, limit=limit, sort=sort)
        # Vector half only augments *discover* (a real query); never browse, never on
        # explicit sort (caller asked for recency, not relevance), never when degraded.
        terms = [t for t in re.findall(r"[A-Za-z0-9_.:/-]{3,}", query or "")]
        if not self._vec_ok or not terms or sort:
            return "\n".join(f"- {c}" for c in kw_list)
        try:
            vec = await asyncio.get_event_loop().run_in_executor(
                None, self._vector_contents, query, norm, limit)
        except Exception as e:  # fail-open to keyword-only
            logger.debug("local-vector: vector search skipped: %s", e)
            return "\n".join(f"- {c}" for c in kw_list)
        merged = self._rrf_merge([kw_list, vec], limit)
        return "\n".join(f"- {c}" for c in merged)

    async def prefetch(self, query: str, *, session_id: str, user_id=None) -> str:
        # Keyword half computed DIRECTLY (not via super().prefetch — that calls
        # self.search(), which polymorphically dispatches to THIS class's hybrid
        # override and would run the vector KNN + embed twice per prefetch). Same
        # contract as the base prefetch: anon-blocked -> "", terms-only, NO
        # browse-on-empty (automatic prefetch must not inject recent rows).
        if self._anon_blocked(user_id):
            return ""
        norm = self._norm_user(user_id)
        terms = [t for t in re.findall(r"[A-Za-z0-9_.:/-]{3,}", query or "")]
        kw_list = (
            self._keyword_contents(query, norm_user=norm, limit=self.top_k, allow_browse=False)
            if terms else []
        )
        kw = "\n".join(f"- {c}" for c in kw_list)
        if not self._vec_ok or not (query or "").strip():
            return kw
        # Vector recall fires on any non-empty query (semantic), even when keyword
        # finds no >=3-char terms — this is where semantic beats keyword.
        try:
            vec = await asyncio.get_event_loop().run_in_executor(
                None, self._vector_contents, query, norm, self.top_k)
        except Exception as e:  # fail-open to keyword-only
            logger.debug("local-vector: prefetch vector skipped: %s", e)
            return kw
        if not vec:
            return kw
        merged = self._rrf_merge([kw_list, vec], self.top_k)
        return "\n".join(f"- {c}" for c in merged)

    # ---- KB (knowledge-base) vector overrides (Task 5) ----------------------

    async def kb_ingest_chunk(self, *, user_id, collection: str, source_path: str,
                              source_hash: str, chunk_idx: int, content: str,
                              mime: str = "text/plain", created_at: str = None) -> bool:
        """FTS write (super) + vector write when _vec_ok. Fail-open: vector failure
        must not prevent the FTS row from being written. Returns the FTS write's
        success bool (the vector half is best-effort and never flips it).
        """
        # FTS half (always) — its bool is the authoritative success signal.
        fts_ok = await super().kb_ingest_chunk(
            user_id=user_id, collection=collection, source_path=source_path,
            source_hash=source_hash, chunk_idx=chunk_idx, content=content,
            mime=mime, created_at=created_at,
        )
        if not self._vec_ok or self._anon_blocked(user_id):
            return fts_ok
        content_stripped = (content or "").strip()
        if not content_stripped:
            return fts_ok
        try:
            emb = await self._embed(content_stripped)
            norm = self._norm_user(user_id)
            await asyncio.get_event_loop().run_in_executor(
                None, self._kb_vec_write,
                norm, collection, source_path, str(chunk_idx), content_stripped, emb,
            )
        except Exception as e:  # fail-open: FTS row already persisted
            logger.debug("local-vector: kb vector write skipped: %s", e)
        return fts_ok

    def _kb_vec_write(self, norm_user: str, collection: str, source_path: str,
                      chunk_idx: str, content: str, emb: List[float]) -> None:
        import sqlite_vec
        con = vec_connect(self.db_path)
        try:
            with con:
                cur = con.cursor()
                cur.execute(
                    "INSERT INTO kb_meta "
                    "(user_id, collection, source_path, chunk_idx, content) "
                    "VALUES (?,?,?,?,?)",
                    (norm_user, collection, source_path, chunk_idx, content),
                )
                rowid = con.last_insert_rowid()
                cur.execute(
                    "INSERT INTO kb_vec (rowid, user_id, embedding) VALUES (?,?,?)",
                    (rowid, norm_user, sqlite_vec.serialize_float32(emb)),
                )
        finally:
            con.close()

    def _kb_vector_contents(self, query: str, norm_user: str, collection: str,
                            limit: int) -> List[tuple]:
        """Tenant+collection-scoped KNN over kb_vec joined to kb_meta.

        Returns list of (content, source_path, chunk_idx) tuples, best first.
        """
        import sqlite_vec
        emb = self._embed_sync(query)
        con = vec_connect(self.db_path)
        try:
            # kb_vec is partitioned by user_id ONLY, so `k` limits the KNN across the
            # tenant's ENTIRE vector set BEFORE the m.collection filter is applied. If
            # the true k nearest are dominated by another collection, the requested
            # collection loses recall (down to zero). Over-fetch k, then slice the
            # collection-filtered rows down to `limit` so semantic recall is restored.
            overfetch = min(max(limit * _KB_VEC_OVERFETCH, limit), _KB_VEC_OVERFETCH_CAP)
            rows = con.cursor().execute(
                "SELECT m.content, m.source_path, m.chunk_idx, v.distance "
                "FROM kb_vec v "
                "JOIN kb_meta m ON m.rowid = v.rowid "
                "WHERE v.user_id = ? AND m.collection = ? "
                "AND v.embedding MATCH ? AND k = ? "
                "ORDER BY v.distance",
                (norm_user, collection, sqlite_vec.serialize_float32(emb), overfetch),
            ).fetchall()
            cutoff = _max_distance()
            filtered = [
                (r[0], r[1], r[2])
                for r in rows
                if r[3] is not None and r[3] <= cutoff
            ]
            return filtered[:limit]
        finally:
            con.close()

    async def kb_search(self, query: str, *, user_id, collection: str = "default",
                        limit: int = 8) -> str:
        """Hybrid KB recall: keyword + vector merged via RRF. Fail-open to keyword-only."""
        if self._anon_blocked(user_id):
            return ""
        norm = self._norm_user(user_id)
        kw_rows = self.kb_keyword_contents(query, user_id=user_id, collection=collection,
                                           limit=limit)
        # Only run vector when vec is healthy AND query has real content
        terms = [t for t in re.findall(r"[A-Za-z0-9_.:/-]{3,}", query or "")]
        if not self._vec_ok or not (query or "").strip():
            # FTS-only (no vec or empty query)
            if not kw_rows:
                return ""
            return "\n".join(f"[{src} #{idx}] {content}" for content, src, idx in kw_rows)
        try:
            vec_rows = await asyncio.get_event_loop().run_in_executor(
                None, self._kb_vector_contents, query, norm, collection, limit)
        except Exception as e:  # fail-open to keyword-only
            logger.debug("local-vector: kb vector search skipped: %s", e)
            vec_rows = []
        if not vec_rows:
            if not kw_rows:
                return ""
            return "\n".join(f"[{src} #{idx}] {content}" for content, src, idx in kw_rows)
        # Build content-keyed ranked lists for RRF (content string is the unique key).
        kw_contents = [content for content, _, _ in kw_rows]
        vec_contents = [content for content, _, _ in vec_rows]
        merged_contents = self._rrf_merge([kw_contents, vec_contents], limit)
        # Re-attach provenance: build a lookup from content → (source_path, chunk_idx).
        # Prefer kw_rows provenance; fall back to vec_rows if only in vec.
        provenance: dict = {}
        for content, src, idx in vec_rows:
            provenance[content] = (src, idx)
        for content, src, idx in kw_rows:
            provenance[content] = (src, idx)
        lines = []
        for content in merged_contents:
            src, idx = provenance.get(content, ("?", "?"))
            lines.append(f"[{src} #{idx}] {content}")
        return "\n".join(lines)

    async def kb_remove(self, *, user_id, collection: str, source: str = None) -> int:
        """FTS remove (super) + delete matching kb_meta/kb_vec rows."""
        removed = await super().kb_remove(user_id=user_id, collection=collection,
                                          source=source)
        if not self._vec_ok or self._anon_blocked(user_id):
            return removed
        norm = self._norm_user(user_id)
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, self._kb_vec_remove, norm, collection, source)
        except Exception as e:
            logger.debug("local-vector: kb vector remove skipped: %s", e)
        return removed

    def _kb_vec_remove(self, norm_user: str, collection: str, source: str = None) -> None:
        """Delete kb_meta rows (+ cascades to kb_vec via rowid) for a source/collection."""
        con = vec_connect(self.db_path)
        try:
            with con:
                cur = con.cursor()
                if source is not None:
                    meta_rows = cur.execute(
                        "SELECT rowid FROM kb_meta "
                        "WHERE user_id = ? AND collection = ? AND source_path = ?",
                        (norm_user, collection, source),
                    ).fetchall()
                else:
                    meta_rows = cur.execute(
                        "SELECT rowid FROM kb_meta WHERE user_id = ? AND collection = ?",
                        (norm_user, collection),
                    ).fetchall()
                for row in meta_rows:
                    rowid = row[0]
                    cur.execute("DELETE FROM kb_vec WHERE rowid = ?", (rowid,))
                    cur.execute("DELETE FROM kb_meta WHERE rowid = ?", (rowid,))
        finally:
            con.close()
