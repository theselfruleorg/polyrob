"""Tenant-scoped knowledge-base chunks + source tracking (``kb_chunks`` FTS5 + ``kb_sources``) — never touched by ``sync_turn``.

Split out of ``modules/memory/sqlite_memory_provider.py`` (S5, 2026-08-29). A mixin: it
relies on the host provider for ``db_path``, ``_norm_user``/``_anon_blocked``,
``_run_blocking``, ``_row_cap``/``_clamp_limit``/``_fts_match``/``_query_terms`` and the
module ``logger``; ``SqliteMemoryProvider`` composes it and calls ``_init_kb_schema`` from
``_init_schema`` inside the same connection.
"""
import logging
from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger("modules.memory.sqlite_memory_provider")


class KbStoreMixin:
    def _init_kb_schema(self, conn) -> None:
        """Create/migrate this store's tables on an open connection (no commit)."""
        # KB tables (Task 5): tenant-scoped knowledge-base chunks + source tracking.
        # Separate from the conversational mem_* tables — never touched by sync_turn.
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks "
            "USING fts5("
            "  user_id UNINDEXED, collection UNINDEXED, "
            "  source_path UNINDEXED, chunk_idx UNINDEXED, "
            "  content"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kb_sources ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id TEXT NOT NULL, "
            "collection TEXT NOT NULL, "
            "source_path TEXT NOT NULL, "
            "source_hash TEXT NOT NULL, "
            "chunk_count INTEGER NOT NULL DEFAULT 0, "
            "mime TEXT, "
            "created_at TEXT, "
            "UNIQUE(user_id, collection, source_path)"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS kb_sources_tenant "
            "ON kb_sources (user_id, collection)"
        )

    async def kb_ingest_chunk(self, *, user_id, collection: str, source_path: str,
                              source_hash: str, chunk_idx: int, content: str,
                              mime: str = "text/plain", created_at: str = None) -> bool:
        """Insert a chunk into kb_chunks FTS + upsert kb_sources counts.

        Anon-blocked / empty-content → no-op, returns False. Returns True on a
        successful write, False on a DB error — so callers can detect a partial
        ingest instead of silently marking a half-written file as complete.
        """
        if self._anon_blocked(user_id):
            return False
        norm = self._norm_user(user_id)
        content = (content or "").strip()
        if not content:
            return False
        try:
            execute_retry(
                self.db_path,
                "INSERT INTO kb_chunks (user_id, collection, source_path, chunk_idx, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (norm, collection, source_path, str(chunk_idx), content),
            )
            execute_retry(
                self.db_path,
                "INSERT INTO kb_sources "
                "(user_id, collection, source_path, source_hash, chunk_count, mime, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(user_id, collection, source_path) DO UPDATE SET "
                "source_hash = excluded.source_hash, "
                "chunk_count = chunk_count + 1, "
                "mime = excluded.mime, "
                "created_at = excluded.created_at",
                (norm, collection, source_path, source_hash, mime, created_at),
            )
            return True
        except Exception as e:
            logger.warning("kb_ingest_chunk failed: %s", e)
            return False
    def kb_keyword_contents(self, query: str, *, user_id, collection: str,
                            limit: int) -> list:
        """FTS5 recall over kb_chunks scoped to (user_id, collection).

        Returns a list of (content, source_path, chunk_idx) tuples.
        Uses the same token sanitizer as _keyword_contents. Empty query →
        recent browse (rowid DESC). Never raises — returns [] on error.
        """
        norm = self._norm_user(user_id)
        limit = self._clamp_limit(limit, 8)
        try:
            terms = self._query_terms(query)
            if terms:
                match = self._fts_match(terms)
                rows = execute_retry(
                    self.db_path,
                    "SELECT content, source_path, chunk_idx FROM kb_chunks "
                    "WHERE kb_chunks MATCH ? AND user_id = ? AND collection = ? "
                    "ORDER BY rank LIMIT ?",
                    (match, norm, collection, limit),
                    fetch="all",
                )
            elif (query or "").strip():
                # A non-empty query that yields no usable terms must NOT fall back to
                # recent-browse — that returns chunks unrelated to what was asked for.
                # Only a truly-empty query browses recent (the list-most-recent path).
                return []
            else:
                rows = execute_retry(
                    self.db_path,
                    "SELECT content, source_path, chunk_idx FROM kb_chunks "
                    "WHERE user_id = ? AND collection = ? "
                    "ORDER BY rowid DESC LIMIT ?",
                    (norm, collection, limit),
                    fetch="all",
                )
            return [(r["content"], r["source_path"], r["chunk_idx"]) for r in (rows or [])]
        except Exception as e:
            logger.warning("kb_keyword_contents failed: %s", e)
            return []
    async def kb_search(self, query: str, *, user_id, collection: str = "default",
                        limit: int = 8) -> str:
        """FTS-only KB recall. Returns provenance-tagged '[source_path #chunk_idx] content'
        lines joined by newline, or '' on anon-block / no results.
        """
        if self._anon_blocked(user_id):
            return ""
        rows = self.kb_keyword_contents(query, user_id=user_id, collection=collection,
                                        limit=limit)
        if not rows:
            return ""
        return "\n".join(f"[{src} #{idx}] {content}" for content, src, idx in rows)
    async def kb_list_sources(self, *, user_id, collection: str = None) -> list:
        """Return list of source dicts for this tenant, optionally filtered by collection.

        Each dict has: user_id, collection, source_path, source_hash, chunk_count, mime,
        created_at. Returns [] on anon-block or error.
        """
        if self._anon_blocked(user_id):
            return []
        norm = self._norm_user(user_id)
        try:
            if collection is not None:
                rows = execute_retry(
                    self.db_path,
                    "SELECT user_id, collection, source_path, source_hash, chunk_count, "
                    "mime, created_at FROM kb_sources "
                    "WHERE user_id = ? AND collection = ? ORDER BY id",
                    (norm, collection), fetch="all",
                )
            else:
                rows = execute_retry(
                    self.db_path,
                    "SELECT user_id, collection, source_path, source_hash, chunk_count, "
                    "mime, created_at FROM kb_sources "
                    "WHERE user_id = ? ORDER BY id",
                    (norm,), fetch="all",
                )
            return [dict(r) for r in (rows or [])]
        except Exception as e:
            logger.warning("kb_list_sources failed: %s", e)
            return []
    async def kb_remove(self, *, user_id, collection: str, source: str = None) -> int:
        """Remove kb_chunks (and kb_sources entry) for a source or whole collection.

        Returns the number of chunk rows removed. Anon-blocked → 0.
        """
        if self._anon_blocked(user_id):
            return 0
        norm = self._norm_user(user_id)
        try:
            if source is not None:
                # Count first (FTS5 DELETE doesn't return affected rows reliably)
                count_rows = execute_retry(
                    self.db_path,
                    "SELECT COUNT(*) AS n FROM kb_chunks "
                    "WHERE user_id = ? AND collection = ? AND source_path = ?",
                    (norm, collection, source), fetch="all",
                )
                count = count_rows[0]["n"] if count_rows else 0
                execute_retry(
                    self.db_path,
                    "DELETE FROM kb_chunks "
                    "WHERE user_id = ? AND collection = ? AND source_path = ?",
                    (norm, collection, source),
                )
                execute_retry(
                    self.db_path,
                    "DELETE FROM kb_sources "
                    "WHERE user_id = ? AND collection = ? AND source_path = ?",
                    (norm, collection, source),
                )
            else:
                count_rows = execute_retry(
                    self.db_path,
                    "SELECT COUNT(*) AS n FROM kb_chunks "
                    "WHERE user_id = ? AND collection = ?",
                    (norm, collection), fetch="all",
                )
                count = count_rows[0]["n"] if count_rows else 0
                execute_retry(
                    self.db_path,
                    "DELETE FROM kb_chunks WHERE user_id = ? AND collection = ?",
                    (norm, collection),
                )
                execute_retry(
                    self.db_path,
                    "DELETE FROM kb_sources WHERE user_id = ? AND collection = ?",
                    (norm, collection),
                )
            return count
        except Exception as e:
            logger.warning("kb_remove failed: %s", e)
            return 0
    def kb_source_hash(self, *, user_id, collection: str, source_path: str):
        """Return the stored source_hash for this (user_id, collection, source_path),
        or None if not found. Synchronous — cheap SELECT, no async needed.
        """
        if self._anon_blocked(user_id):
            return None
        norm = self._norm_user(user_id)
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT source_hash FROM kb_sources "
                "WHERE user_id = ? AND collection = ? AND source_path = ?",
                (norm, collection, source_path), fetch="all",
            )
            if rows:
                return rows[0]["source_hash"]
            return None
        except Exception as e:
            logger.warning("kb_source_hash failed: %s", e)
            return None
