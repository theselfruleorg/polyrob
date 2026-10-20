"""Keyword (FTS5) recall rows for ``SqliteMemoryProvider`` — split out of the
provider file (file-size ratchet) when the 2026-10-13 memory/coherency review
added reader-aware ranking, recency, supersession and dated vector hits.

The mixin relies on the provider's ``db_path``. ``_keyword_rows`` is the ONE
FTS5 read shape (discover / browse / ``before_id`` pagination); the ranking of
its candidate pool lives in ``modules.memory.recall_rank``.
"""
import logging
import time

from core.sqlite_util import execute_retry
from modules.memory import scope as _scope

logger = logging.getLogger("modules.memory.sqlite_memory_provider")


class KeywordRecallMixin:
    """FTS5 recall: tokenizer, MATCH builder, ranked rows, line formatting."""

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
        from modules.memory.fts_terms import tokens
        return tokens(query)

    @staticmethod
    def _fts_match(terms: list) -> str:
        """Quoted (injection-safe) FTS5 OR-query over <=12 informative terms (B1, fts_terms)."""
        from modules.memory.fts_terms import select_terms
        return " OR ".join(f'"{t}"' for t in select_terms(terms))

    def _keyword_rows(self, query: str, *, norm_user: str, limit: int,
                      sort: str = None, allow_browse: bool = True,
                      exclude_session_id: str = None, before_id: int = None,
                      scope=None, demote_automation: bool = True,
                      prefer_job_id: str = None, prefer_goal_id: str = None) -> list:
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

        2026-10-13 review (D2, A3, A7): the demotion is now the READER's choice
        (``demote_automation`` — the agents tier passes its regime's
        ``demote_automation_recall``; True keeps the legacy order for a caller
        that names nothing). A cron / goal reader passes ``prefer_job_id`` /
        ``prefer_goal_id`` and its own past runs (episode ``meta.job_id`` /
        ``goal_id``) rank first. The default rank branch fetches a candidate
        pool and ``modules.memory.recall_rank.rerank`` orders it by group, then
        relevance x recency, and keeps only the newer of two near-duplicates.

        Provenance is fetched in a SECOND query by rowid (not a JOIN) — FTS5 MATCH
        does not compose reliably with JOIN/aliasing, and rank ordering must stay
        exactly as before.
        """
        terms = self._query_terms(query)
        _excl_sql = " AND m.session_id != ?" if exclude_session_id else ""
        _excl_arg = (exclude_session_id,) if exclude_session_id else ()
        _before_sql = " AND m.rowid < ?" if before_id is not None else ""
        _before_arg = (before_id,) if before_id is not None else ()
        # 025: the scope predicate rides the cursor slot on all three shapes.
        _sc_sql, _sc_arg = _scope.read_predicate(scope, norm_user)
        _before_sql, _before_arg = _before_sql + _sc_sql, _before_arg + _sc_arg
        if terms:
            match = self._fts_match(terms)
            ranked = sort not in ("newest", "oldest")
            if not ranked:
                order = "m.rowid DESC" if sort == "newest" else "m.rowid ASC"
                cols, col_arg, fetch_n = "", (), limit
            else:
                own = bool(prefer_job_id or prefer_goal_id)
                cols = (
                    ", rank AS rnk, (EXISTS (SELECT 1 FROM episodes e WHERE e.session_id = "
                    "m.session_id AND e.user_id = m.user_id "
                    "AND e.kind IN ('cron','goal'))) AS auto_flag"
                    + (", (EXISTS (SELECT 1 FROM episodes e WHERE e.session_id = "
                       "m.session_id AND e.user_id = m.user_id AND (e.thread_key IN (?, ?) "
                       "OR e.goal_id = ? OR (json_valid(e.meta) AND json_extract(e.meta, '$.job_id') = ?)))) AS own_flag"
                       if own else ""))
                # WS2 run records key a run as thread_key "cron:<job>" / "goal:<goal>";
                # older episodes carry goal_id / meta.job_id.
                col_arg = ((f"cron:{prefer_job_id}" if prefer_job_id else None,
                            f"goal:{prefer_goal_id}" if prefer_goal_id else None,
                            prefer_goal_id, prefer_job_id) if own else ())
                order = ("own_flag DESC, " if own else "") + (
                    "auto_flag ASC, " if demote_automation else "") + "rank ASC"
                # A pool, so recency and supersession can reorder inside it.
                fetch_n = min(max(limit * 4, limit), 40)
            rows = execute_retry(
                self.db_path,
                f"SELECT m.rowid AS rowid, m.content AS content{cols} FROM memories m "
                f"WHERE memories MATCH ? AND m.user_id = ?{_excl_sql}{_before_sql} "
                f"ORDER BY {order} LIMIT ?",
                col_arg + (match, norm_user) + _excl_arg + _before_arg + (fetch_n,),
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
        out = [{"content": r["content"], "ts": ts_by_rowid.get(r["rowid"]),
                "rowid": r["rowid"]} for r in rows]
        if terms and sort not in ("newest", "oldest"):
            from modules.memory.recall_rank import rerank
            keys = rows[0].keys() if rows else ()
            for o, r in zip(out, rows):
                o["rank"] = r["rnk"] if "rnk" in keys else None
                o["auto"] = bool(r["auto_flag"]) if "auto_flag" in keys else False
                o["own"] = bool(r["own_flag"]) if "own_flag" in keys else False
            out = rerank(out, limit=limit, demote_automation=demote_automation)
        return out

    def _dated_existing(self, norm_user: str, contents: list) -> dict:
        """D11: ``{content: ts}`` for the contents that still exist as ``memories``
        rows of this tenant (``ts`` None when the row has no stamp). A vector-only
        hit is looked up here so it renders with its date, and a hit whose keyword
        row was pruned or purged (a vector twin left behind) is NOT returned."""
        contents = [c for c in dict.fromkeys(contents or []) if c]
        if not contents:
            return {}
        marks = ",".join("?" for _ in contents)
        rows = execute_retry(
            self.db_path,
            f"SELECT m.rowid AS rowid, m.content AS content FROM memories m "
            f"WHERE m.user_id = ? AND m.content IN ({marks})",
            (norm_user,) + tuple(contents), fetch="all") or []
        found = {r["content"]: r["rowid"] for r in rows}
        ts = {}
        if found:
            try:
                ids = tuple(found.values())
                prows = execute_retry(
                    self.db_path,
                    "SELECT mem_rowid, ts FROM mem_provenance WHERE mem_rowid IN "
                    f"({','.join('?' for _ in ids)})", ids, fetch="all") or []
                ts = {p["mem_rowid"]: p["ts"] for p in prows}
            except Exception as e:  # provenance is additive
                logger.debug("mem provenance lookup skipped: %s", e)
        return {c: ts.get(rid) for c, rid in found.items()}

    def _reader_flags(self, norm_user: str, contents: list, *, prefer_job_id: str = None,
                      prefer_goal_id: str = None) -> dict:
        """``{content: (own, auto)}`` for vector-only hits — the SAME two flags
        ``_keyword_rows`` computes in SQL, so the hybrid merge can keep the
        reader's group order (an RRF merge alone puts a cron row the KNN liked
        above every owner-chat row). A content with no ``memories`` row is absent."""
        contents = [c for c in dict.fromkeys(contents or []) if c]
        if not contents:
            return {}
        own = bool(prefer_job_id or prefer_goal_id)
        own_sql = (", (EXISTS (SELECT 1 FROM episodes e WHERE e.session_id = m.session_id "
                   "AND e.user_id = m.user_id AND (e.thread_key IN (?, ?) OR e.goal_id = ? "
                   "OR (json_valid(e.meta) AND json_extract(e.meta, '$.job_id') = ?)))) AS own_flag" if own else "")
        own_arg = ((f"cron:{prefer_job_id}" if prefer_job_id else None,
                    f"goal:{prefer_goal_id}" if prefer_goal_id else None,
                    prefer_goal_id, prefer_job_id) if own else ())
        rows = execute_retry(
            self.db_path,
            "SELECT m.content AS content, (EXISTS (SELECT 1 FROM episodes e WHERE "
            "e.session_id = m.session_id AND e.user_id = m.user_id "
            f"AND e.kind IN ('cron','goal'))) AS auto_flag{own_sql} FROM memories m "
            f"WHERE m.user_id = ? AND m.content IN ({','.join('?' for _ in contents)})",
            own_arg + (norm_user,) + tuple(contents), fetch="all") or []
        out = {}
        for r in rows:
            prev = out.get(r["content"], (False, False))
            out[r["content"]] = (prev[0] or (bool(r["own_flag"]) if own else False),
                                 prev[1] or bool(r["auto_flag"]))
        return out

    def _keyword_contents(self, query: str, *, norm_user: str, limit: int,
                          sort: str = None, allow_browse: bool = True,
                          exclude_session_id: str = None, scope=None) -> list:
        """Bare content strings (subclass RRF contract — the hybrid vector provider
        merges ranked lists keyed by content). Delegates to `_keyword_rows`."""
        return [r["content"] for r in self._keyword_rows(
            query, norm_user=norm_user, limit=limit, sort=sort,
            allow_browse=allow_browse, exclude_session_id=exclude_session_id, scope=scope)]

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
