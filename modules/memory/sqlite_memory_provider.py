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

from modules.memory.wikilinks import _WIKILINK_RE, parse_wikilinks  # noqa: F401  (re-exported)
from modules.memory.sqlite_curated_store import CuratedNotesStoreMixin
from modules.memory.sqlite_kb_store import KbStoreMixin
from modules.memory.sqlite_episodes_store import EpisodeStoreMixin


def _require_user_id() -> bool:
    """Whether empty-user_id memory I/O is refused (default true = safe-by-construction).

    BEHAVIOR FIX (task-1.4): old variant ``in ("1","true","yes")`` treated any value
    outside that set (e.g. ``=on``) as False. Converged to bool_env canonical falsey-set
    so ``=on``/``=yes``/``=1`` are all truthy and ``=off``/``=none``/``=false`` are all
    falsy — matches the documented contract.
    """
    return bool_env("MEMORY_REQUIRE_USER_ID", True)


class SqliteMemoryProvider(CuratedNotesStoreMixin, KbStoreMixin, EpisodeStoreMixin, MemoryProvider):
    """Turn store + FTS5 recall core; the curated-notes, KB and episode stores are mixins
    (S5 split, 2026-08-29) sharing this class's connection helpers and tenant guards."""

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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_prov_user_hash "
                         "ON mem_provenance(user_id, content_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_prov_ts "
                         "ON mem_provenance(ts)")
            self._init_curated_schema(conn)
            self._init_kb_schema(conn)
            self._init_episodes_schema(conn)
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
        if cls._store_answer_only():
            content = (assistant_content or "").strip()
        else:
            content = f"User: {user_content}\nAssistant: {assistant_content}".strip()
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
                        session_id: str, user_id=None):
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
            self._sync_turn_blocking, self._norm_user(user_id), session_id, content)

    def _sync_turn_blocking(self, norm_user: str, session_id: str, content: str) -> bool:
        """Write one memory row + its provenance stamp (B2). Exact duplicates
        collapse: an existing (user, content_hash) provenance row gets its ts
        refreshed instead of inserting a twin — the store stops growing on
        repeated identical findings. Provenance failures never lose the memory
        row (fail-open, like every other leg of this provider). Returns True on
        a real insert, False on a dup collapse."""
        import hashlib
        content_hash = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        try:
            dup = execute_retry(
                self.db_path,
                "SELECT mem_rowid FROM mem_provenance "
                "WHERE user_id = ? AND content_hash = ? LIMIT 1",
                (norm_user, content_hash), fetch="one")
            if dup is not None:
                execute_retry(
                    self.db_path,
                    "UPDATE mem_provenance SET ts = ? WHERE mem_rowid = ?",
                    (int(time.time()), dup["mem_rowid"]))
                return False
        except Exception as e:  # dedup probe failure -> fall through to plain insert
            logger.debug("mem dedup probe skipped: %s", e)
        rowid = execute_retry(
            self.db_path,
            "INSERT INTO memories (user_id, session_id, content) VALUES (?, ?, ?)",
            (norm_user, session_id, content), fetch="lastrowid")
        try:
            execute_retry(
                self.db_path,
                "INSERT OR REPLACE INTO mem_provenance "
                "(mem_rowid, user_id, ts, kind, content_hash) VALUES (?,?,?,?,?)",
                (rowid, norm_user, int(time.time()), "finding", content_hash))
        except Exception as e:  # provenance is additive — never lose the memory row
            logger.debug("mem provenance stamp skipped: %s", e)
        return True

    @staticmethod
    def _clamp_limit(limit, default: int) -> int:
        """Clamp a caller-supplied result count to [1, 20]; bad input => default."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = default
        return max(1, min(20, limit))

    @staticmethod
    def _query_terms(query: str) -> list:
        """Significant (>=3-char) tokens from a free-text query — the ONE tokenizer
        for every FTS5 recall path (base provider + the vector subclass)."""
        return re.findall(r"[A-Za-z0-9_.:/-]{3,}", query or "")

    @staticmethod
    def _fts_match(terms: list) -> str:
        """Sanitized FTS5 OR-query over the first 12 terms (quoting each term
        disables FTS query syntax injection)."""
        return " OR ".join(f'"{t}"' for t in terms[:12])

    async def search(self, query: str, *, user_id=None, session_id: str = None,
                     limit: int = 5, sort: str = None, before_id: int = None,
                     with_ids: bool = False) -> str:
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
        """
        if self._anon_blocked(user_id):
            return ""
        limit = self._clamp_limit(limit, self.top_k)
        try:
            # M3: offload the (blocking) FTS query off the event loop.
            rows = await self._run_blocking(
                self._keyword_rows,
                query, norm_user=self._norm_user(user_id), limit=limit, sort=sort,
                before_id=before_id)
        except Exception as e:
            logger.warning("sqlite memory search failed: %s", e)
            return ""
        return self._format_recall_rows(rows, with_ids=with_ids)

    def _keyword_rows(self, query: str, *, norm_user: str, limit: int,
                      sort: str = None, allow_browse: bool = True,
                      exclude_session_id: str = None, before_id: int = None) -> list:
        """FTS5 recall -> ranked list of ``{"content", "ts", "rowid"}`` dicts (no
        formatting). ``ts`` comes from the B2 provenance sidecar (None for legacy
        rows). Discover when `query` has >=3-char terms; otherwise browse
        most-recent (unless allow_browse=False -> []).

        P2-1: when `exclude_session_id` is set (the automatic prefetch passes the
        CURRENT session), rows written by that session are excluded — otherwise recall
        re-injects the session's OWN just-written findings (already in context via the
        H-MEM tail) as 'untrusted external' memory, wasting tokens and top-k slots.

        T2.6: `before_id` (rowid cursor) is applied as a plain `rowid < ?` WHERE
        filter regardless of `sort` — for sort="newest" that's exactly "strictly
        older, same order" (honest forward pagination); for the default rank order
        it narrows the MATCH candidates BEFORE ranking (keeps rank order) — this
        is LOSSY, not just reordered: a match that ranks between the returned page
        and the cursor, but below either, is permanently excluded from every
        subsequent page (proven: a rank-ordered page1 of ids [2, 4] then
        before_id=2 can never again surface an id like 3 or 5 that ranked between
        them). This is exactly why the agent-facing action (T2.6 review fix) only
        advertises the before_id hint for sort="newest" — the one lossless mode.
        For sort="oldest" the filter runs the SAME direction as the ORDER BY
        (ascending), so it is NOT forward-pagination in that mode either — no
        extra cleverness is applied to make it one (see plan T2.6: "adapt
        honestly for other sorts").

        T2.6 automation-source demotion: on the default rank-ordered (no explicit
        `sort`) MATCH branch, a row whose session has a completed 'cron'/'goal'
        episode for the SAME tenant (`modules/memory/episodic.py::finalize_episode`)
        sorts AFTER every interactive ('chat'/no-episode) row, rank preserved
        within each group. This is the only reliable existing source-of-origin
        signal — `memories` rows carry no kind/source column, and cron/goal
        sessions use the SAME `create_session` id scheme as chat (no naming
        convention to key off). Episodes only exist when EPISODIC_MEMORY_ENABLED
        is on (default off outside POLYROB_LOCAL/autonomous posture); with the
        ledger empty this EXISTS subquery is always false, i.e. a no-op — never
        applied to sort="newest"/"oldest" (an explicit time-order request is
        honored as asked) or to browse (no MATCH -> no `rank` to blend with).

        Provenance is fetched in a SECOND query by rowid (not a JOIN) — FTS5 MATCH
        does not compose reliably with JOIN/aliasing, and rank ordering must stay
        exactly as before.
        """
        terms = self._query_terms(query)
        _excl_sql = " AND m.session_id != ?" if exclude_session_id else ""
        _excl_arg = (exclude_session_id,) if exclude_session_id else ()
        _before_sql = " AND m.rowid < ?" if before_id is not None else ""
        _before_arg = (before_id,) if before_id is not None else ()
        if terms:
            match = self._fts_match(terms)
            if sort in ("newest", "oldest"):
                order = "m.rowid DESC" if sort == "newest" else "m.rowid ASC"
            else:
                order = (
                    "(EXISTS (SELECT 1 FROM episodes e WHERE e.session_id = "
                    "m.session_id AND e.user_id = m.user_id "
                    "AND e.kind IN ('cron','goal'))) ASC, rank ASC"
                )
            rows = execute_retry(
                self.db_path,
                f"SELECT m.rowid AS rowid, m.content AS content FROM memories m "
                f"WHERE memories MATCH ? AND m.user_id = ?{_excl_sql}{_before_sql} "
                f"ORDER BY {order} LIMIT ?",
                (match, norm_user) + _excl_arg + _before_arg + (limit,),
                fetch="all",
            )
        elif allow_browse:
            order = "m.rowid ASC" if sort == "oldest" else "m.rowid DESC"
            rows = execute_retry(
                self.db_path,
                f"SELECT m.rowid AS rowid, m.content AS content FROM memories m "
                f"WHERE m.user_id = ?{_excl_sql}{_before_sql} "
                f"ORDER BY {order} LIMIT ?",
                (norm_user,) + _excl_arg + _before_arg + (limit,),
                fetch="all",
            )
        else:
            return []
        rows = rows or []
        ts_by_rowid = {}
        if rows:
            try:
                ids = [r["rowid"] for r in rows]
                marks = ",".join("?" for _ in ids)
                prows = execute_retry(
                    self.db_path,
                    f"SELECT mem_rowid, ts FROM mem_provenance WHERE mem_rowid IN ({marks})",
                    tuple(ids), fetch="all")
                ts_by_rowid = {p["mem_rowid"]: p["ts"] for p in (prows or [])}
            except Exception as e:  # provenance is additive — recall must not break
                logger.debug("mem provenance lookup skipped: %s", e)
        return [{"content": r["content"], "ts": ts_by_rowid.get(r["rowid"]),
                 "rowid": r["rowid"]} for r in rows]

    def _keyword_contents(self, query: str, *, norm_user: str, limit: int,
                          sort: str = None, allow_browse: bool = True,
                          exclude_session_id: str = None) -> list:
        """Bare content strings (subclass RRF contract — the hybrid vector provider
        merges ranked lists keyed by content). Delegates to `_keyword_rows`."""
        return [r["content"] for r in self._keyword_rows(
            query, norm_user=norm_user, limit=limit, sort=sort,
            allow_browse=allow_browse, exclude_session_id=exclude_session_id)]

    @staticmethod
    def _recall_line(content: str, ts=None, rowid=None) -> str:
        """One recall bullet; date-prefixed when the write-time stamp is known (B2).
        T2.6: an ``(id N)`` suffix when `rowid` is given (opt-in, session_search
        pagination) — omitted (legacy shape, byte-identical) when `rowid` is None."""
        if ts:
            try:
                day = time.strftime("%Y-%m-%d", time.localtime(int(ts)))
                line = f"- [{day}] {content}"
            except Exception:
                line = f"- {content}"
        else:
            line = f"- {content}"
        if rowid is not None:
            line = f"{line} (id {rowid})"
        return line

    @classmethod
    def _format_recall_rows(cls, rows, *, with_ids: bool = False) -> str:
        return "\n".join(
            cls._recall_line(r["content"], r.get("ts"), r.get("rowid") if with_ids else None)
            for r in rows
        )

    async def prefetch(self, query: str, *, session_id: str, user_id=None) -> str:
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
                limit=self.top_k, exclude_session_id=session_id)
        except Exception as e:
            logger.warning("sqlite memory prefetch failed: %s", e)
            return ""
        return self._format_recall_rows(rows)

    _PRUNE_BATCH = 500  # ids per DELETE ... IN (...) — safely under any param limit

    def prune_memories(self, *, older_than_ts: int) -> int:
        """Age-based retention for the cross-session store (B3), across ALL tenants.
        Deletes memories rows whose B2 provenance stamp is older than the cutoff
        (+ the stamp itself). Legacy rows WITHOUT a provenance stamp are exempt —
        their age is unknowable, and guessing risks deleting live recall. Called
        from the curator tick on its own cadence, NEVER from the write path.
        Fail-open: any DB error degrades to 0 rather than raising.
        """
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT mem_rowid FROM mem_provenance WHERE ts < ?",
                (int(older_than_ts),), fetch="all")
            ids = [r["mem_rowid"] for r in (rows or [])]
            if not ids:
                return 0
            # Batched IN clauses: a multi-year backlog can exceed SQLite's bound-
            # parameter limit (999 on older builds), and the resulting error would
            # be swallowed fail-open — retention silently broken forever.
            for i in range(0, len(ids), self._PRUNE_BATCH):
                chunk = tuple(ids[i:i + self._PRUNE_BATCH])
                marks = ",".join("?" for _ in chunk)
                execute_retry(self.db_path,
                              f"DELETE FROM memories WHERE rowid IN ({marks})",
                              chunk)
                execute_retry(self.db_path,
                              f"DELETE FROM mem_provenance WHERE mem_rowid IN ({marks})",
                              chunk)
            return len(ids)
        except Exception as e:
            logger.warning("prune_memories failed: %s", e)
            return 0


    # ---- notes substrate (C1, 2026-07-11) ------------------------------------
    # curated_memory promoted to first-class notes: title/tags/[[wikilinks]]/
    # provenance/status lifecycle. All verbs tenant-scoped and fail-open; caps
    # ride the existing MEMORY_TOOL_MAX_ENTRIES/MAX_CHARS knobs (archived notes
    # do NOT count against the entry cap — archiving frees space).


    _NOTE_FIELDS = ("id, title, content, tags, links, source, created_ts, "
                    "updated_ts, access_count, status, created_by")


    # ---- KB (knowledge-base) storage methods (Task 5) ----------------------


