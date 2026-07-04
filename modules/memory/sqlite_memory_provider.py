"""Cross-session memory backend: SQLite FTS5 keyword recall over stored turns.

Implements the MemoryProvider ABC. Durable, multi-process-safe (WAL + jittered
retry via core/sqlite_util), no external services. Default-ON via MEMORY_BACKEND=sqlite
(see backend_factory). FTS5 keyword recall mirrors Reference's session search; a vector
layer can be added later behind the same interface without touching callers.

Multi-tenant safety (UP-03): an empty/anonymous user_id would otherwise collapse into a
single shared "" recall bucket. With the backend default-ON, any caller that doesn't set
user_id (CLI/local/misconfigured paths) would read & write that shared bucket. So when
MEMORY_REQUIRE_USER_ID is true (the default), empty-user_id read/writes are SKIPPED
(with a one-time warning) rather than bucketed. Single-user/local deployments can set
MEMORY_REQUIRE_USER_ID=false to restore the shared-"" convenience.
"""
import asyncio
import functools
import json
import logging
import os
import re
import time
from typing import Optional

from core.env import bool_env
from core.identity import is_anonymous, normalize_user_id
from core.sqlite_util import execute_retry, wal_connect
from modules.memory.provider import MemoryProvider

logger = logging.getLogger(__name__)


def _require_user_id() -> bool:
    """Whether empty-user_id memory I/O is refused (default true = safe-by-construction).

    BEHAVIOR FIX (task-1.4): old variant ``in ("1","true","yes")`` treated any value
    outside that set (e.g. ``=on``) as False. Converged to bool_env canonical falsey-set
    so ``=on``/``=yes``/``=1`` are all truthy and ``=off``/``=none``/``=false`` are all
    falsy — matches the documented contract.
    """
    return bool_env("MEMORY_REQUIRE_USER_ID", True)


class SqliteMemoryProvider(MemoryProvider):
    def __init__(self, db_path: str, *, top_k: int = 5):
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.db_path = db_path
        self.top_k = top_k
        self._warned_empty_user = False
        self._init_schema()

    def _init_schema(self) -> None:
        conn = wal_connect(self.db_path)
        try:
            # Schema carries user_id so recall can be tenant-scoped (P0-0). An older
            # dark table (pre-user_id) is rebuilt: the backend ships default-OFF, so
            # there is no production data to migrate — drop and recreate is safe.
            cols = self._table_columns(conn)
            if cols and "user_id" not in cols:
                conn.execute("DROP TABLE IF EXISTS memories")
                conn.commit()
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories "
                "USING fts5(user_id UNINDEXED, session_id UNINDEXED, content)"
            )
            # UP-09: curated per-tenant notes for the optional `memory` tool. Plain
            # table (no FTS) — small, read-in-full, agent-curated. Tenant-scoped by user_id.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS curated_memory ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, content TEXT)"
            )
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
            # Episodic activity ledger (2026-07-03): one durable, time-ordered row per
            # completed run (chat/goal/cron). Plain B-tree table (NOT fts5) so ts is a
            # real indexed column and "last 8 hours" is a range scan. Separate from the
            # relevance `memories` store — never touched by sync_turn/prefetch.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS episodes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts INTEGER NOT NULL, started_ts INTEGER, "
                "user_id TEXT NOT NULL, session_id TEXT NOT NULL, thread_key TEXT, "
                "kind TEXT NOT NULL, task TEXT, outcome TEXT, summary TEXT, "
                "artifacts TEXT NOT NULL DEFAULT '[]', spend_usd REAL NOT NULL DEFAULT 0, "
                "steps INTEGER NOT NULL DEFAULT 0, goal_id TEXT, "
                "surfaced INTEGER NOT NULL DEFAULT 0, meta TEXT NOT NULL DEFAULT '{}', "
                "created_at INTEGER NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_user_ts "
                         "ON episodes(user_id, ts DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_user_kind_ts "
                         "ON episodes(user_id, kind, ts DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_user_thread "
                         "ON episodes(user_id, thread_key, ts DESC)")
            # Composite key (user_id, session_id) — NOT session_id alone. A caller
            # can supply an arbitrary session_id, so two different tenants sharing
            # one session_id string must get two rows, not a cross-tenant merge
            # (mirrors the kb_sources UNIQUE(user_id, collection, source_path) pattern).
            #
            # Migrate any pre-fix single-column unique index (idx_episodes_session ON
            # episodes(session_id)) to the tenant-composite key. CREATE ... IF NOT EXISTS
            # matches by NAME only, so a stale single-column index under the old name would
            # otherwise survive and break ON CONFLICT(user_id, session_id). Drop it, then
            # create the composite under a NEW name. Safe: episodes ships dark (no rows on
            # any real install), and the old UNIQUE(session_id) guaranteed no cross-tenant
            # dupes exist to block the looser composite.
            conn.execute("DROP INDEX IF EXISTS idx_episodes_session")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_tenant_session "
                         "ON episodes(user_id, session_id)")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _table_columns(conn) -> list:
        """Return the column names of the `memories` table, or [] if it doesn't exist."""
        try:
            rows = conn.execute("PRAGMA table_info(memories)").fetchall()
            return [r[1] for r in rows]
        except Exception:
            return []

    @staticmethod
    def _norm_user(user_id) -> str:
        """Normalize the tenant key. None/empty collapse to a single shared bucket so
        single-user/local recall still works; named users are isolated from each other
        and from the anonymous bucket. Delegates to the identity SSOT."""
        return normalize_user_id(user_id)

    def _anon_blocked(self, user_id) -> bool:
        """True when an anonymous/default user_id must NOT touch the shared bucket (UP-03).

        Anonymity is decided by the identity SSOT ``is_anonymous`` — so the canonical
        ``_anonymous_`` token and the synthetic server sentinels are refused too, not
        just the empty string (findings F1). A real named tenant (e.g. the CLI's
        ``local``) is never blocked. Emits a one-time warning so the misconfiguration
        is visible (ties to UP-01 #3).
        """
        if not is_anonymous(user_id):
            return False
        if not _require_user_id():
            return False  # single-user/local opt-out: allow the shared anon bucket
        if not self._warned_empty_user:
            self._warned_empty_user = True
            logger.warning(
                "sqlite memory: skipping recall I/O for an anonymous/default user_id "
                "(MEMORY_REQUIRE_USER_ID=true). Set a real user_id, or set "
                "MEMORY_REQUIRE_USER_ID=false for single-user/local deployments."
            )
        return True

    @property
    def name(self) -> str:
        return "sqlite-fts"

    @property
    def is_external(self) -> bool:
        return True

    @staticmethod
    def _store_answer_only() -> bool:
        """Phase 1.1: store the ANSWER (distilled findings) as the FTS-matched/embedded
        content instead of the "User: {q}\nAssistant: {a}" transcript. Indexing the
        echoed question made a recall query (which restates the question) rank the
        question text as highly as the answer. Default ON under POLYROB_LOCAL (soak),
        OFF on the multi-tenant server until soaked; explicit MEMORY_STORE_ANSWER_ONLY
        wins. Read directly from env so this module never imports agents.task."""
        return bool_env("MEMORY_STORE_ANSWER_ONLY", bool_env("POLYROB_LOCAL", False))

    @classmethod
    def _compose_stored_content(cls, user_content: str, assistant_content: str) -> str:
        """Build the row content per the answer-only policy. Shared by the keyword and
        vector providers so both store the SAME string (keeps RRF dedup-by-content
        consistent across the two halves)."""
        if cls._store_answer_only():
            return (assistant_content or "").strip()
        return f"User: {user_content}\nAssistant: {assistant_content}".strip()

    @staticmethod
    async def _run_blocking(fn, *args, **kwargs):
        """M3: run a blocking sqlite call off the event loop. execute_retry opens a fresh
        connection and, under WAL write contention, does a real time.sleep() retry loop
        (up to ~2s) — running it inline in these async hot-path methods would freeze the
        ENTIRE loop (every concurrent session), not just the calling coroutine."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    async def sync_turn(self, user_content: str, assistant_content: str, *,
                        session_id: str, user_id=None) -> None:
        content = self._compose_stored_content(user_content, assistant_content)
        if not content:
            return
        if self._anon_blocked(user_id):
            return
        await self._run_blocking(
            execute_retry,
            self.db_path,
            "INSERT INTO memories (user_id, session_id, content) VALUES (?, ?, ?)",
            (self._norm_user(user_id), session_id, content),
        )

    @staticmethod
    def _clamp_limit(limit, default: int) -> int:
        """Clamp a caller-supplied result count to [1, 20]; bad input => default."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = default
        return max(1, min(20, limit))

    async def search(self, query: str, *, user_id=None, session_id: str = None,
                     limit: int = 5, sort: str = None) -> str:
        """Tenant-scoped recall (UP-09). Two shapes inferred from args:

        - **discover** (`query` has terms): FTS5 MATCH over a sanitized OR-query,
          ordered by `rank` (default) or `rowid` for sort="newest"/"oldest".
        - **browse** (`query` empty/no significant terms): the most-recent (or oldest)
          rows for this tenant, no MATCH — answers "what was I working on".

        Always scoped via `AND user_id = ?`; refuses empty user_id under
        MEMORY_REQUIRE_USER_ID (same guard as prefetch). `limit` clamped to [1,20].
        Returns newline-joined "- {content}" snippets, or "" on no results / refusal.
        """
        if self._anon_blocked(user_id):
            return ""
        limit = self._clamp_limit(limit, self.top_k)
        try:
            # M3: offload the (blocking) FTS query off the event loop.
            contents = await self._run_blocking(
                self._keyword_contents,
                query, norm_user=self._norm_user(user_id), limit=limit, sort=sort)
        except Exception as e:
            logger.warning("sqlite memory search failed: %s", e)
            return ""
        return "\n".join(f"- {c}" for c in contents)

    def _keyword_contents(self, query: str, *, norm_user: str, limit: int,
                          sort: str = None, allow_browse: bool = True) -> list:
        """FTS5 recall -> ranked list of content strings (no formatting). Subclasses
        (e.g. the hybrid vector provider) reuse this to RRF-merge with other signals
        without lossy re-parsing of a joined string. Discover when `query` has >=3-char
        terms; otherwise browse most-recent (unless allow_browse=False -> [])."""
        terms = [t for t in re.findall(r"[A-Za-z0-9_.:/-]{3,}", query or "")]
        if terms:
            match = " OR ".join(f'"{t}"' for t in terms[:12])
            order = {"newest": "rowid DESC", "oldest": "rowid ASC"}.get(sort, "rank")
            rows = execute_retry(
                self.db_path,
                f"SELECT content FROM memories WHERE memories MATCH ? AND user_id = ? "
                f"ORDER BY {order} LIMIT ?",
                (match, norm_user, limit),
                fetch="all",
            )
        elif allow_browse:
            order = "rowid ASC" if sort == "oldest" else "rowid DESC"
            rows = execute_retry(
                self.db_path,
                f"SELECT content FROM memories WHERE user_id = ? ORDER BY {order} LIMIT ?",
                (norm_user, limit),
                fetch="all",
            )
        else:
            return []
        return [r["content"] for r in (rows or [])]

    async def prefetch(self, query: str, *, session_id: str, user_id=None) -> str:
        # Thin caller of search() preserving the exact legacy shape: rank-ordered,
        # top_k results, "" on anon-block or no significant terms (NO browse-on-empty —
        # automatic prefetch must not inject recent rows when the query is empty).
        if self._anon_blocked(user_id):
            return ""
        terms = [t for t in re.findall(r"[A-Za-z0-9_.:/-]{3,}", query or "")]
        if not terms:
            return ""
        return await self.search(query, user_id=user_id, session_id=session_id,
                                  limit=self.top_k)

    # ---- curated per-tenant store (UP-09 `memory` tool) ----------------------
    @staticmethod
    def _curated_caps():
        try:
            max_entries = int(os.getenv("MEMORY_TOOL_MAX_ENTRIES", "50"))
        except ValueError:
            max_entries = 50
        try:
            max_chars = int(os.getenv("MEMORY_TOOL_MAX_CHARS", "2000"))
        except ValueError:
            max_chars = 2000
        return max_entries, max_chars

    async def curated_add(self, user_id, content: str) -> bool:
        """Add a curated note for this tenant. Returns False on anon-refusal, empty
        content, over-char-cap, or over-entry-cap (so the tool can report the reason)."""
        if self._anon_blocked(user_id):
            return False
        content = (content or "").strip()
        if not content:
            return False
        max_entries, max_chars = self._curated_caps()
        if len(content) > max_chars:
            return False
        norm = self._norm_user(user_id)
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT COUNT(*) AS n FROM curated_memory WHERE user_id = ?",
                (norm,), fetch="all",
            )
            if rows and rows[0]["n"] >= max_entries:
                return False
            execute_retry(
                self.db_path,
                "INSERT INTO curated_memory (user_id, content) VALUES (?, ?)",
                (norm, content),
            )
            return True
        except Exception as e:
            logger.warning("curated_add failed: %s", e)
            return False

    async def curated_read(self, user_id) -> str:
        """Return this tenant's curated notes as newline-joined "- {content}" (or "")."""
        if self._anon_blocked(user_id):
            return ""
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT content FROM curated_memory WHERE user_id = ? ORDER BY id",
                (self._norm_user(user_id),), fetch="all",
            )
        except Exception as e:
            logger.warning("curated_read failed: %s", e)
            return ""
        return "\n".join(f"- {r['content']}" for r in (rows or []))

    async def curated_remove(self, user_id, substring: str) -> int:
        """Delete this tenant's curated notes containing `substring`. Returns count."""
        if self._anon_blocked(user_id):
            return 0
        substring = (substring or "").strip()
        if not substring:
            return 0
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT id FROM curated_memory WHERE user_id = ? AND content LIKE ?",
                (self._norm_user(user_id), f"%{substring}%"), fetch="all",
            )
            ids = [r["id"] for r in (rows or [])]
            for _id in ids:
                execute_retry(self.db_path,
                              "DELETE FROM curated_memory WHERE id = ?", (_id,))
            return len(ids)
        except Exception as e:
            logger.warning("curated_remove failed: %s", e)
            return 0

    # ---- KB (knowledge-base) storage methods (Task 5) ----------------------

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
            terms = [t for t in re.findall(r"[A-Za-z0-9_.:/-]{3,}", query or "")]
            if terms:
                match = " OR ".join(f'"{t}"' for t in terms[:12])
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

    # ---- episodic activity ledger (2026-07-03) -----------------------------
    @staticmethod
    def _episode_to_record(r) -> "EpisodeRecord":
        from modules.memory.provider import EpisodeRecord
        try:
            artifacts = json.loads(r["artifacts"]) if r["artifacts"] else []
        except Exception:
            artifacts = []
        try:
            meta = json.loads(r["meta"]) if r["meta"] else None
        except Exception:
            meta = None
        return EpisodeRecord(
            ts=r["ts"], started_ts=r["started_ts"], user_id=r["user_id"],
            session_id=r["session_id"], thread_key=r["thread_key"], kind=r["kind"],
            task=r["task"], outcome=r["outcome"], summary=r["summary"],
            artifacts=artifacts, spend_usd=r["spend_usd"], steps=r["steps"],
            goal_id=r["goal_id"], meta=meta,
        )

    @staticmethod
    def _safe_artifacts_json(artifacts, cap: int = 8000) -> str:
        """Serialize the artifacts list, capped at `cap` chars WITHOUT ever producing
        invalid JSON. Character-slicing a serialized JSON string (the old behavior)
        can cut mid-token, and a parse failure on read silently drops the WHOLE list
        to []. Instead: if the full serialization is oversize, drop trailing elements
        and re-serialize until it fits — the stored value is always valid JSON, and
        a partial (but non-empty, non-corrupt) list beats total data loss.
        """
        items = list(artifacts or [])
        try:
            serialized = json.dumps(items)
        except Exception:
            return "[]"
        # Re-serializes the whole (shrinking) list on every dropped element — O(n^2)
        # in the number of trimmed items. Acceptable at realistic artifact-list sizes
        # (a handful to a few dozen entries); not worth the complexity of a smarter
        # incremental trim for this cold, best-effort truncation path.
        while len(serialized) > cap and items:
            items = items[:-1]
            try:
                serialized = json.dumps(items)
            except Exception:
                return "[]"
        if len(serialized) > cap:
            # Even an empty list "shouldn't" exceed the cap, but never emit
            # something over-cap or invalid.
            return "[]"
        return serialized

    @staticmethod
    def _safe_meta_json(meta, cap: int = 4000) -> str:
        """Serialize the meta dict; if it exceeds `cap` chars, store "{}" (valid JSON)
        rather than a character-sliced (and therefore corrupt) string. Meta is
        supplementary/best-effort, so dropping an oversize blob wholesale is
        preferable to shipping unparseable data.
        """
        try:
            serialized = json.dumps(meta or {})
        except Exception:
            return "{}"
        if len(serialized) > cap:
            return "{}"
        return serialized

    async def record_episode(self, episode, *, session_id: str, user_id=None) -> None:
        if self._anon_blocked(user_id):
            return
        norm = self._norm_user(user_id)
        try:
            artifacts = self._safe_artifacts_json(episode.artifacts)
            meta = self._safe_meta_json(episode.meta)
            execute_retry(
                self.db_path,
                "INSERT INTO episodes (ts, started_ts, user_id, session_id, thread_key, "
                "kind, task, outcome, summary, artifacts, spend_usd, steps, goal_id, "
                "meta, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id, session_id) DO UPDATE SET "
                "ts=excluded.ts, outcome=excluded.outcome, summary=excluded.summary, "
                "artifacts=excluded.artifacts, "
                "spend_usd=MAX(episodes.spend_usd, excluded.spend_usd), "
                "steps=MAX(episodes.steps, excluded.steps), meta=excluded.meta",
                (int(episode.ts), episode.started_ts, norm, session_id, episode.thread_key,
                 episode.kind, (episode.task or "")[:1000], episode.outcome,
                 (episode.summary or "")[:2000], artifacts, float(episode.spend_usd or 0),
                 int(episode.steps or 0), episode.goal_id, meta, int(time.time())),
            )
        except Exception as e:
            logger.warning("record_episode failed: %s", e)

    async def recall_episodes(self, *, user_id=None, since_ts=None, until_ts=None,
                              kind=None, thread_key=None, limit=20, order="newest",
                              exclude_surfaced: bool = False) -> list:
        if self._anon_blocked(user_id):
            return []
        limit = self._clamp_limit(limit, 20)
        direction = "ASC" if order == "oldest" else "DESC"
        where = ["user_id = ?"]
        args = [self._norm_user(user_id)]
        if since_ts is not None:
            where.append("ts >= ?"); args.append(int(since_ts))
        if until_ts is not None:
            where.append("ts <= ?"); args.append(int(until_ts))
        if kind:
            where.append("kind = ?"); args.append(kind)
        if thread_key:
            where.append("thread_key = ?"); args.append(thread_key)
        if exclude_surfaced:
            where.append("surfaced = 0")
        args.append(limit)
        try:
            rows = execute_retry(
                self.db_path,
                f"SELECT ts, started_ts, user_id, session_id, thread_key, kind, task, "
                f"outcome, summary, artifacts, spend_usd, steps, goal_id, meta "
                f"FROM episodes WHERE {' AND '.join(where)} "
                f"ORDER BY ts {direction} LIMIT ?",
                tuple(args), fetch="all",
            )
        except Exception as e:
            logger.warning("recall_episodes failed: %s", e)
            return []
        return [self._episode_to_record(r) for r in (rows or [])]

    def prune_episodes(self, *, older_than_ts: int) -> int:
        """Delete episodes older than the cutoff, across ALL tenants. Returns rows
        removed. Cheap indexed delete (ts is a real B-tree column, see
        idx_episodes_user_ts) — called from the curator tick on its own cadence,
        NEVER from the write path. Fail-open: any DB error degrades to 0 rather
        than raising, so a retention hiccup never breaks the curator tick.
        """
        try:
            rows = execute_retry(
                self.db_path, "SELECT COUNT(*) AS n FROM episodes WHERE ts < ?",
                (int(older_than_ts),), fetch="all")
            n = rows[0]["n"] if rows else 0
            execute_retry(self.db_path, "DELETE FROM episodes WHERE ts < ?",
                          (int(older_than_ts),))
            return n
        except Exception as e:
            logger.warning("prune_episodes failed: %s", e)
            return 0

    def mark_episode_surfaced(self, *, session_id: str, user_id: Optional[str] = None) -> None:
        """Mark the episode(s) for this session_id as already delivered out-of-band
        (cron/self-wake), so a subsequent digest recall (``exclude_surfaced=True``)
        doesn't re-surface it. Fail-open (log + swallow) — a marking failure must
        never break the delivery path that calls this.

        The episodes table is keyed on the COMPOSITE ``(user_id, session_id)`` —
        two tenants can legitimately share the same session_id string. When
        ``user_id`` is provided the UPDATE is scoped to that tenant only, so a
        collision can't flip another tenant's row. ``user_id=None`` keeps the
        legacy session_id-only UPDATE for back-compat callers.
        """
        try:
            if user_id is not None:
                execute_retry(
                    self.db_path,
                    "UPDATE episodes SET surfaced = 1 WHERE session_id = ? AND user_id = ?",
                    (session_id, self._norm_user(user_id)))
            else:
                execute_retry(self.db_path,
                              "UPDATE episodes SET surfaced = 1 WHERE session_id = ?",
                              (session_id,))
        except Exception as e:
            logger.warning("mark_episode_surfaced failed: %s", e)
