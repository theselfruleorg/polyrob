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
import time
from typing import Optional

from core.context_fences import strip_turn_fences
from core.env import bool_env
from core.identity import is_anonymous, normalize_user_id
from core.sqlite_util import execute_retry, wal_connect
from modules.memory.provider import MemoryProvider

logger = logging.getLogger(__name__)

from modules.memory.wikilinks import _WIKILINK_RE, parse_wikilinks  # noqa: F401  (re-exported)
from modules.memory.sqlite_curated_store import CuratedNotesStoreMixin
from modules.memory.recall_consolidation import RecallConsolidationMixin
from modules.memory.sqlite_kb_store import KbStoreMixin
from modules.memory.sqlite_episodes_store import EpisodeStoreMixin
from modules.memory.sqlite_scope_store import ScopeStoreMixin
from modules.memory.sqlite_recall import KeywordRecallMixin
from modules.memory import scope as _scope


def _require_user_id() -> bool:
    """Whether empty-user_id memory I/O is refused (default true = safe-by-construction).

    Parsed with the canonical ``bool_env`` falsey-set, never a private truth set.
    """
    return bool_env("MEMORY_REQUIRE_USER_ID", True)


class SqliteMemoryProvider(KeywordRecallMixin, CuratedNotesStoreMixin, KbStoreMixin,
                           EpisodeStoreMixin, RecallConsolidationMixin, ScopeStoreMixin,
                           MemoryProvider):
    """Turn store + FTS5 recall core; curated notes, KB, episodes and recall
    consolidation are mixins sharing this class's connection helpers and guards."""

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
            # Provenance sidecar (B2, 2026-07-11): FTS5 can't be ALTERed, so the
            # write-time metadata the store was missing (D1 — no timestamp, no kind)
            # lives in a plain table keyed by the FTS rowid. NOT named `mem_meta` —
            # that name is the local_vector provider's vector sidecar. content_hash
            # powers exact-dup collapse at write; legacy rows simply have no row here
            # and render unprefixed.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mem_provenance ("
                "mem_rowid INTEGER PRIMARY KEY, user_id TEXT, "
                "ts INTEGER NOT NULL, kind TEXT, content_hash TEXT)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_prov_ts "
                         "ON mem_provenance(ts)")
            self._init_scope_schema(conn)  # 025: scope column + widened dedup index
            self._init_curated_schema(conn)
            self._init_kb_schema(conn)
            self._init_episodes_schema(conn)
            from modules.memory.storage_limits import install_regular_limits
            install_regular_limits(conn)
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

    @staticmethod
    def _row_cap() -> int:
        """Max chars per stored memory row (D8). Episodes and curated notes already
        cap their writes; the auto-injected `memories` rows did not, so one oversized
        tool dump became a permanent recall-bloat row. <=0 disables."""
        try:
            return int(os.getenv("MEMORY_ROW_MAX_CHARS", "4000"))
        except ValueError:
            return 4000

    @classmethod
    def _compose_stored_content(cls, user_content: str, assistant_content: str) -> str:
        """Build the row content per the answer-only policy. Shared by the keyword and
        vector providers so both store the SAME string (keeps RRF dedup-by-content
        consistent across the two halves). The D8 cap is applied HERE for the same
        reason — a truncated FTS row must match its embedded twin byte-for-byte."""
        u, a = strip_turn_fences(user_content, assistant_content)  # 070 W1.5: no fence is stored
        content = ("" if not (a or "").strip() else a.strip() if cls._store_answer_only()
                   else f"User: {u}\nAssistant: {a}".strip())
        from core.secret_scrub import scrub_secret_shapes
        content = scrub_secret_shapes(content)
        cap = cls._row_cap()
        if cap > 0 and len(content) > cap:
            content = content[:cap]
        return content

    @staticmethod
    async def _run_blocking(fn, *args, **kwargs):
        """M3: run a blocking sqlite call off the event loop. execute_retry opens a fresh
        connection and, under WAL write contention, does a real time.sleep() retry loop
        (up to ~2s) — running it inline in these async hot-path methods would freeze the
        ENTIRE loop (every concurrent session), not just the calling coroutine."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    async def sync_turn(self, user_content: str, assistant_content: str, *,
                        session_id: str, user_id=None, scope=None):
        """Returns True when a new row was inserted, False when the write was an
        exact duplicate (collapsed into a ts refresh), None on early-out — so the
        vector subclass can skip embedding a collapsed dup. Callers through the
        registry ignore the return value (ABC contract stays None-compatible)."""
        content = self._compose_stored_content(user_content, assistant_content)
        if not content:
            return None
        if self._anon_blocked(user_id):
            return None
        return await self._run_blocking(
            self._sync_turn_blocking, self._norm_user(user_id), session_id, content,
            _scope.write_label(scope))

    def _sync_turn_blocking(self, norm_user: str, session_id: str, content: str,
                            label: str = "") -> bool:
        """Write one memory row + its provenance stamp (B2). Exact duplicates
        collapse: an existing (user, content_hash) provenance row gets its ts
        refreshed instead of inserting a twin — the store stops growing on
        repeated identical findings. Provenance failures never lose the memory
        row (fail-open, like every other leg of this provider). Returns True on
        a real insert, False on a dup collapse."""
        import hashlib
        content_hash = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        # 025: with scopes ON the dedup key is per-scope — a scoped finding must
        # never collapse into a ts refresh of a SHARED twin (a quarantine bypass).
        on = _scope.scopes_enabled()
        try:
            dup = execute_retry(
                self.db_path,
                "SELECT mem_rowid FROM mem_provenance WHERE user_id = ? "
                + ("AND scope = ? " if on else "") + "AND content_hash = ? LIMIT 1",
                (norm_user,) + ((label,) if on else ()) + (content_hash,), fetch="one")
            if dup is not None:
                execute_retry(
                    self.db_path,
                    "UPDATE mem_provenance SET ts = ? WHERE mem_rowid = ?",
                    (int(time.time()), dup["mem_rowid"]))
                return False
        except Exception as e:  # dedup probe failure -> fall through to plain insert
            logger.debug("mem dedup probe skipped: %s", e)
        from modules.memory.storage_limits import insert_finding
        rowid = insert_finding(self.db_path, norm_user, session_id, content)
        try:
            execute_retry(
                self.db_path,
                "INSERT OR REPLACE INTO mem_provenance "
                "(mem_rowid, user_id, ts, kind, content_hash, scope) VALUES (?,?,?,?,?,?)",
                (rowid, norm_user, int(time.time()), "finding", content_hash, label or ""))
        except Exception as e:  # provenance is additive — never lose the memory row
            logger.debug("mem provenance stamp skipped: %s", e)
        return True

    async def search(self, query: str, *, user_id=None, session_id: str = None,
                     limit: int = 5, sort: str = None, before_id: int = None,
                     with_ids: bool = False, scope=None, demote_automation: bool = True,
                     prefer_job_id: str = None, prefer_goal_id: str = None) -> str:
        """Tenant-scoped recall (UP-09). Two shapes inferred from args:

        - **discover** (`query` has terms): FTS5 MATCH over a sanitized OR-query,
          ordered by `rank` (default) or `rowid` for sort="newest"/"oldest".
        - **browse** (`query` empty/no significant terms): the most-recent (or oldest)
          rows for this tenant, no MATCH — answers "what was I working on".

        Always scoped via `AND user_id = ?`; refuses empty user_id under
        MEMORY_REQUIRE_USER_ID (same guard as prefetch). `limit` clamped to [1,20].

        T2.6 (2026-07-22): `before_id` is a rowid cursor for pagination — only rows
        with `rowid < before_id` are considered, under whatever ordering `sort`
        already selects (see `_keyword_rows`). `with_ids=True` appends an
        ``(id N)`` suffix to each line (the id a caller can pass back as the next
        `before_id`); default False keeps the legacy exact ``"- {content}"``
        format byte-identical for direct callers that don't request it.

        Returns newline-joined "- {content}" snippets, or "" on no results / refusal.
        A READ FAILURE RAISES (D3, review 2026-10-13): "" means "nothing matched",
        never "the store could not be read" — every caller names the failure.
        """
        if self._anon_blocked(user_id):
            return ""
        limit = self._clamp_limit(limit, self.top_k)
        try:
            # M3: offload the (blocking) FTS query off the event loop.
            rows = await self._run_blocking(
                self._keyword_rows,
                query, norm_user=self._norm_user(user_id), limit=limit, sort=sort,
                before_id=before_id, scope=scope, demote_automation=demote_automation,
                prefer_job_id=prefer_job_id, prefer_goal_id=prefer_goal_id)
        except Exception as e:
            logger.warning("sqlite memory search failed: %s", e)
            raise
        return self._format_recall_rows(rows, with_ids=with_ids)

    async def prefetch(self, query: str, *, session_id: str, user_id=None,
                       scope=None, demote_automation: bool = True,
                       prefer_job_id: str = None, prefer_goal_id: str = None) -> str:
        # Rank-ordered, top_k, "" on anon-block or no significant terms (NO browse-on-
        # empty — automatic prefetch must not inject recent rows when the query is empty).
        # P2-1: calls _keyword_contents DIRECTLY (not search()) so it can exclude the
        # CURRENT session — the explicit search action stays all-sessions.
        if self._anon_blocked(user_id):
            return ""
        terms = self._query_terms(query)
        if not terms:
            return ""
        try:
            rows = await self._run_blocking(
                self._keyword_rows, query, norm_user=self._norm_user(user_id),
                limit=self.top_k, exclude_session_id=session_id, scope=scope,
                demote_automation=demote_automation, prefer_job_id=prefer_job_id,
                prefer_goal_id=prefer_goal_id)
        except Exception as e:
            logger.warning("sqlite memory prefetch failed: %s", e)
            raise  # D3: an unreadable store is not an empty one
        return self._format_recall_rows(rows)

    # ---- notes substrate (C1, 2026-07-11) ------------------------------------
    # curated_memory promoted to first-class notes: title/tags/[[wikilinks]]/
    # provenance/status lifecycle. All verbs tenant-scoped and fail-open; caps
    # ride the existing MEMORY_TOOL_MAX_ENTRIES/MAX_CHARS knobs (archived notes
    # do NOT count against the entry cap — archiving frees space).


    _NOTE_FIELDS = ("id, title, content, tags, links, source, created_ts, "
                    "updated_ts, access_count, status, created_by")


    # ---- KB (knowledge-base) storage methods (Task 5) ----------------------
