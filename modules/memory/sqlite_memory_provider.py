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

# C1 (2026-07-11): Obsidian-style [[wikilink]] targets inside note bodies.
_WIKILINK_RE = re.compile(r"\[\[([^\]\[]+)\]\]")


def parse_wikilinks(text) -> list:
    """Extract [[wikilink]] targets from a note body, in order. '' / None -> []."""
    if not text:
        return []
    return _WIKILINK_RE.findall(str(text))


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
            # UP-09: curated per-tenant notes for the optional `memory` tool. Plain
            # table (no FTS) — small, read-in-full, agent-curated. Tenant-scoped by user_id.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS curated_memory ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, content TEXT)"
            )
            # C1 (2026-07-11): promote curated_memory to a notes substrate — additive
            # column migration (plain table, ALTERable; a pre-C1 table is widened in
            # place, existing rows read as active legacy notes via COALESCE).
            note_cols = (
                ("title", "TEXT"),
                ("tags", "TEXT"),            # JSON list
                ("links", "TEXT"),           # JSON list of [[wikilink]] targets
                ("source", "TEXT"),          # provenance: session/episode/skill id
                ("created_ts", "INTEGER"),
                ("updated_ts", "INTEGER"),
                ("access_count", "INTEGER DEFAULT 0"),
                ("status", "TEXT DEFAULT 'active'"),   # active|pending|archived
                ("created_by", "TEXT"),
            )
            have = {r[1] for r in
                    conn.execute("PRAGMA table_info(curated_memory)").fetchall()}
            for col, decl in note_cols:
                if col not in have:
                    conn.execute(f"ALTER TABLE curated_memory ADD COLUMN {col} {decl}")
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
            rows = await self._run_blocking(
                self._keyword_rows,
                query, norm_user=self._norm_user(user_id), limit=limit, sort=sort)
        except Exception as e:
            logger.warning("sqlite memory search failed: %s", e)
            return ""
        return self._format_recall_rows(rows)

    def _keyword_rows(self, query: str, *, norm_user: str, limit: int,
                      sort: str = None, allow_browse: bool = True,
                      exclude_session_id: str = None) -> list:
        """FTS5 recall -> ranked list of ``{"content", "ts"}`` dicts (no formatting).
        ``ts`` comes from the B2 provenance sidecar (None for legacy rows). Discover
        when `query` has >=3-char terms; otherwise browse most-recent (unless
        allow_browse=False -> []).

        P2-1: when `exclude_session_id` is set (the automatic prefetch passes the
        CURRENT session), rows written by that session are excluded — otherwise recall
        re-injects the session's OWN just-written findings (already in context via the
        H-MEM tail) as 'untrusted external' memory, wasting tokens and top-k slots.

        Provenance is fetched in a SECOND query by rowid (not a JOIN) — FTS5 MATCH
        does not compose reliably with JOIN/aliasing, and rank ordering must stay
        exactly as before.
        """
        terms = [t for t in re.findall(r"[A-Za-z0-9_.:/-]{3,}", query or "")]
        _excl_sql = " AND session_id != ?" if exclude_session_id else ""
        _excl_arg = (exclude_session_id,) if exclude_session_id else ()
        if terms:
            match = " OR ".join(f'"{t}"' for t in terms[:12])
            order = {"newest": "rowid DESC", "oldest": "rowid ASC"}.get(sort, "rank")
            rows = execute_retry(
                self.db_path,
                f"SELECT rowid, content FROM memories WHERE memories MATCH ? AND user_id = ?"
                f"{_excl_sql} ORDER BY {order} LIMIT ?",
                (match, norm_user) + _excl_arg + (limit,),
                fetch="all",
            )
        elif allow_browse:
            order = "rowid ASC" if sort == "oldest" else "rowid DESC"
            rows = execute_retry(
                self.db_path,
                f"SELECT rowid, content FROM memories WHERE user_id = ?{_excl_sql} "
                f"ORDER BY {order} LIMIT ?",
                (norm_user,) + _excl_arg + (limit,),
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
        return [{"content": r["content"], "ts": ts_by_rowid.get(r["rowid"])}
                for r in rows]

    def _keyword_contents(self, query: str, *, norm_user: str, limit: int,
                          sort: str = None, allow_browse: bool = True,
                          exclude_session_id: str = None) -> list:
        """Bare content strings (subclass RRF contract — the hybrid vector provider
        merges ranked lists keyed by content). Delegates to `_keyword_rows`."""
        return [r["content"] for r in self._keyword_rows(
            query, norm_user=norm_user, limit=limit, sort=sort,
            allow_browse=allow_browse, exclude_session_id=exclude_session_id)]

    @staticmethod
    def _recall_line(content: str, ts=None) -> str:
        """One recall bullet; date-prefixed when the write-time stamp is known (B2)."""
        if ts:
            try:
                day = time.strftime("%Y-%m-%d", time.localtime(int(ts)))
                return f"- [{day}] {content}"
            except Exception:
                pass
        return f"- {content}"

    @classmethod
    def _format_recall_rows(cls, rows) -> str:
        return "\n".join(cls._recall_line(r["content"], r.get("ts")) for r in rows)

    async def prefetch(self, query: str, *, session_id: str, user_id=None) -> str:
        # Rank-ordered, top_k, "" on anon-block or no significant terms (NO browse-on-
        # empty — automatic prefetch must not inject recent rows when the query is empty).
        # P2-1: calls _keyword_contents DIRECTLY (not search()) so it can exclude the
        # CURRENT session — the explicit search action stays all-sessions.
        if self._anon_blocked(user_id):
            return ""
        terms = [t for t in re.findall(r"[A-Za-z0-9_.:/-]{3,}", query or "")]
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
        """Legacy verb: add an active, untitled note. Returns False on anon-refusal,
        empty content, over-char-cap, or over-entry-cap (so the tool can report
        the reason). Delegates to note_create (C1)."""
        return (await self.note_create(user_id, content)) is not None

    async def curated_read(self, user_id) -> str:
        """Return this tenant's ACTIVE curated notes as newline-joined "- {content}"
        (or ""). Pre-C1 rows have status NULL and read as active."""
        if self._anon_blocked(user_id):
            return ""
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT content FROM curated_memory WHERE user_id = ? "
                "AND COALESCE(status, 'active') = 'active' ORDER BY id",
                (self._norm_user(user_id),), fetch="all",
            )
        except Exception as e:
            logger.warning("curated_read failed: %s", e)
            return ""
        return "\n".join(f"- {r['content']}" for r in (rows or []))

    # ---- notes substrate (C1, 2026-07-11) ------------------------------------
    # curated_memory promoted to first-class notes: title/tags/[[wikilinks]]/
    # provenance/status lifecycle. All verbs tenant-scoped and fail-open; caps
    # ride the existing MEMORY_TOOL_MAX_ENTRIES/MAX_CHARS knobs (archived notes
    # do NOT count against the entry cap — archiving frees space).

    @staticmethod
    def _note_row_to_dict(r) -> dict:
        def _json_list(v):
            try:
                out = json.loads(v) if v else []
                return out if isinstance(out, list) else []
            except Exception:
                return []
        return {
            "id": r["id"], "title": r["title"], "content": r["content"],
            "tags": _json_list(r["tags"]), "links": _json_list(r["links"]),
            "source": r["source"], "created_ts": r["created_ts"],
            "updated_ts": r["updated_ts"],
            "access_count": r["access_count"] or 0,
            "status": r["status"] or "active",
            "created_by": r["created_by"] or "agent",
        }

    _NOTE_FIELDS = ("id, title, content, tags, links, source, created_ts, "
                    "updated_ts, access_count, status, created_by")

    @staticmethod
    def _tags_list(tags) -> list:
        if not tags:
            return []
        if isinstance(tags, str):
            return [t.strip() for t in tags.split(",") if t.strip()]
        return [str(t).strip() for t in tags if str(t).strip()]

    async def note_create(self, user_id, content: str, *, title: str = None,
                          tags=None, source: str = None, created_by: str = "agent",
                          status: str = "active"):
        """Create a note; returns its id or None (anon/empty/over-cap/error).
        [[wikilinks]] in the body are parsed into the links column at write."""
        if self._anon_blocked(user_id):
            return None
        content = (content or "").strip()
        if not content:
            return None
        max_entries, max_chars = self._curated_caps()
        if len(content) > max_chars:
            return None
        norm = self._norm_user(user_id)
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT COUNT(*) AS n FROM curated_memory WHERE user_id = ? "
                "AND COALESCE(status, 'active') IN ('active', 'pending')",
                (norm,), fetch="all")
            if rows and rows[0]["n"] >= max_entries:
                return None
            now = int(time.time())
            return execute_retry(
                self.db_path,
                "INSERT INTO curated_memory (user_id, content, title, tags, links, "
                "source, created_ts, updated_ts, access_count, status, created_by) "
                "VALUES (?,?,?,?,?,?,?,?,0,?,?)",
                (norm, content, (title or "").strip() or None,
                 json.dumps(self._tags_list(tags)),
                 json.dumps(parse_wikilinks(content)),
                 source, now, now, status, created_by),
                fetch="lastrowid")
        except Exception as e:
            logger.warning("note_create failed: %s", e)
            return None

    async def note_update(self, user_id, note_id, *, content: str = None,
                          title: str = None, tags=None) -> bool:
        """Update a note's content/title/tags (tenant-scoped). Content updates
        recompute links. Returns False when the note isn't this tenant's."""
        if self._anon_blocked(user_id):
            return False
        sets, args = ["updated_ts = ?"], [int(time.time())]
        if content is not None:
            content = content.strip()
            if not content:
                return False
            _, max_chars = self._curated_caps()
            if len(content) > max_chars:
                return False
            sets += ["content = ?", "links = ?"]
            args += [content, json.dumps(parse_wikilinks(content))]
        if title is not None:
            sets.append("title = ?"); args.append(title.strip() or None)
        if tags is not None:
            sets.append("tags = ?"); args.append(json.dumps(self._tags_list(tags)))
        try:
            n = execute_retry(
                self.db_path,
                f"UPDATE curated_memory SET {', '.join(sets)} "
                f"WHERE id = ? AND user_id = ?",
                tuple(args) + (note_id, self._norm_user(user_id)))
            return bool(n)
        except Exception as e:
            logger.warning("note_update failed: %s", e)
            return False

    async def note_archive(self, user_id, note_id) -> bool:
        """Archive (never delete) a note. Returns False when not this tenant's."""
        if self._anon_blocked(user_id):
            return False
        try:
            n = execute_retry(
                self.db_path,
                "UPDATE curated_memory SET status = 'archived', updated_ts = ? "
                "WHERE id = ? AND user_id = ?",
                (int(time.time()), note_id, self._norm_user(user_id)))
            return bool(n)
        except Exception as e:
            logger.warning("note_archive failed: %s", e)
            return False

    async def note_list(self, user_id, *, status: str = "active", tag: str = None,
                        limit: int = 200) -> list:
        """List this tenant's notes by status (newest updated first), optionally
        filtered by tag. [] on anon-block or error."""
        if self._anon_blocked(user_id):
            return []
        where = ["user_id = ?", "COALESCE(status, 'active') = ?"]
        args = [self._norm_user(user_id), status]
        if tag:
            where.append("tags LIKE ?")
            args.append(f'%{json.dumps(str(tag))[1:-1]}%')
        try:
            rows = execute_retry(
                self.db_path,
                f"SELECT {self._NOTE_FIELDS} FROM curated_memory "
                f"WHERE {' AND '.join(where)} "
                f"ORDER BY COALESCE(updated_ts, 0) DESC, id DESC LIMIT ?",
                tuple(args) + (max(1, min(1000, int(limit or 200))),), fetch="all")
        except Exception as e:
            logger.warning("note_list failed: %s", e)
            return []
        return [self._note_row_to_dict(r) for r in (rows or [])]

    async def note_get(self, user_id, note_id, *, bump_access: bool = True):
        """Fetch one note (tenant-scoped). By default bumps access_count — the
        AGENT-reuse signal the consolidation pass keys staleness on. Passive
        viewers (the read-only webview) pass ``bump_access=False`` so browsing
        the wiki never exempts an agent-unused note from the staleness archive.
        None when absent."""
        if self._anon_blocked(user_id):
            return None
        norm = self._norm_user(user_id)
        try:
            row = execute_retry(
                self.db_path,
                f"SELECT {self._NOTE_FIELDS} FROM curated_memory "
                f"WHERE id = ? AND user_id = ?",
                (note_id, norm), fetch="one")
            if row is None:
                return None
            out = self._note_row_to_dict(row)
            if bump_access:
                execute_retry(
                    self.db_path,
                    "UPDATE curated_memory SET access_count = COALESCE(access_count,0)+1 "
                    "WHERE id = ? AND user_id = ?",
                    (note_id, norm))
                out["access_count"] = (out["access_count"] or 0) + 1
            return out
        except Exception as e:
            logger.warning("note_get failed: %s", e)
            return None

    def consolidate_notes(self, *, stale_before_ts: int) -> dict:
        """Mechanical consolidation (C4), across ALL tenants, curator-tick only:

        - **stale**: archive ACTIVE agent-authored notes never read
          (access_count 0) and not updated since the cutoff. Owner-authored
          (created_by user/owner) and legacy rows (NULL created_by/updated_ts —
          unknowable) are exempt.
        - **dupes**: within (user_id, content) groups of active notes, archive the
          agent-authored copies, keeping the group's single oldest row (an
          owner-authored copy always survives).

        Archive-only (recoverable), fail-open. Returns
        ``{"archived_stale": [(user_id, id)...], "archived_dupes": [...]}`` so the
        curator can emit per-note audit events.
        """
        out = {"archived_stale": [], "archived_dupes": []}
        now = int(time.time())
        try:
            stale = execute_retry(
                self.db_path,
                "SELECT id, user_id FROM curated_memory "
                "WHERE COALESCE(status, 'active') = 'active' "
                "AND created_by IS NOT NULL AND created_by NOT IN ('user', 'owner') "
                "AND COALESCE(access_count, 0) = 0 "
                "AND updated_ts IS NOT NULL AND updated_ts < ?",
                (int(stale_before_ts),), fetch="all") or []
            for r in stale:
                execute_retry(
                    self.db_path,
                    "UPDATE curated_memory SET status = 'archived', updated_ts = ? "
                    "WHERE id = ?", (now, r["id"]))
                out["archived_stale"].append((r["user_id"], r["id"]))
        except Exception as e:
            logger.warning("consolidate_notes stale pass failed: %s", e)
        try:
            groups = execute_retry(
                self.db_path,
                "SELECT user_id, content, MIN(id) AS keep_id FROM curated_memory "
                "WHERE COALESCE(status, 'active') = 'active' "
                "GROUP BY user_id, content HAVING COUNT(*) > 1",
                fetch="all") or []
            for g in groups:
                dupes = execute_retry(
                    self.db_path,
                    "SELECT id, user_id FROM curated_memory "
                    "WHERE user_id = ? AND content = ? AND id != ? "
                    "AND COALESCE(status, 'active') = 'active' "
                    "AND created_by IS NOT NULL AND created_by NOT IN ('user', 'owner')",
                    (g["user_id"], g["content"], g["keep_id"]), fetch="all") or []
                for r in dupes:
                    execute_retry(
                        self.db_path,
                        "UPDATE curated_memory SET status = 'archived', updated_ts = ? "
                        "WHERE id = ?", (now, r["id"]))
                    out["archived_dupes"].append((r["user_id"], r["id"]))
        except Exception as e:
            logger.warning("consolidate_notes dupe pass failed: %s", e)
        return out

    async def note_backlinks(self, user_id, title: str) -> list:
        """Notes whose links list contains `title` (the wiki backlink set)."""
        if self._anon_blocked(user_id) or not (title or "").strip():
            return []
        try:
            rows = execute_retry(
                self.db_path,
                f"SELECT {self._NOTE_FIELDS} FROM curated_memory "
                f"WHERE user_id = ? AND links LIKE ? "
                f"AND COALESCE(status, 'active') != 'archived' ORDER BY id",
                (self._norm_user(user_id),
                 f'%{json.dumps(str(title).strip())}%'), fetch="all")
        except Exception as e:
            logger.warning("note_backlinks failed: %s", e)
            return []
        # LIKE over the JSON is a pre-filter; confirm against the parsed list.
        out = [self._note_row_to_dict(r) for r in (rows or [])]
        return [n for n in out if str(title).strip() in n["links"]]

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
