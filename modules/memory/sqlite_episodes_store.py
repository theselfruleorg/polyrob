"""Episodic activity ledger (``episodes``): one durable, time-ordered row per completed run; recall by window/kind/thread, prune, surfaced marks.

Split out of ``modules/memory/sqlite_memory_provider.py`` (S5, 2026-08-29). A mixin: it
relies on the host provider for ``db_path``, ``_norm_user``/``_anon_blocked``,
``_run_blocking``, ``_row_cap``/``_clamp_limit``/``_fts_match``/``_query_terms`` and the
module ``logger``; ``SqliteMemoryProvider`` composes it and calls ``_init_episodes_schema`` from
``_init_schema`` inside the same connection.
"""
import logging
from core.sqlite_util import execute_retry, wal_connect
from typing import Optional
import json
import re
import time

logger = logging.getLogger("modules.memory.sqlite_memory_provider")


class EpisodeStoreMixin:
    def _init_episodes_schema(self, conn) -> None:
        """Create/migrate this store's tables on an open connection (no commit)."""
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
