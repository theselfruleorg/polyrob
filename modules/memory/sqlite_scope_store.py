"""Scope + retention verbs for the ``memories`` store (``SqliteMemoryProvider`` mixin).

The scope column lives on the ``mem_provenance`` sidecar (FTS5 cannot be
ALTERed) and its vector twin ``mem_meta``. Existing rows read ``scope=''`` =
shared, so the migration moves no data. See :mod:`modules.memory.scope` for the
regime policy; this file holds only storage.

Every verb here is SERVER-side — the goal dispatcher, the curator and the owner
CLI call them. No agent-callable action reaches ``promote_scope`` or
``purge_scope``. Every verb takes ``user_id`` first: a scope is a partition
BELOW the tenant axis, never across it. Fail-open: a DB error returns 0 / [].
"""
import logging
import time
from typing import Dict, List, Optional, Tuple

from core.env import bool_env
from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)


class ScopeStoreMixin:
    """Mixed into ``SqliteMemoryProvider`` (which supplies ``db_path``)."""

    _PRUNE_BATCH = 500  # ids per DELETE ... IN (...) — safely under any param limit

    # ---- schema --------------------------------------------------------------

    @staticmethod
    def _init_scope_schema(conn) -> None:
        """Additive, PRAGMA-guarded (the ``curated_memory`` widening precedent —
        never the DROP path: ``mem_provenance`` holds live rows). The dedup index
        widens to ``(user_id, scope, content_hash)`` under a NEW name, because a
        name-matched ``CREATE INDEX IF NOT EXISTS`` never widens."""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(mem_provenance)").fetchall()}
        if "scope" not in cols:
            conn.execute("ALTER TABLE mem_provenance ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
        conn.execute("DROP INDEX IF EXISTS idx_mem_prov_user_hash")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_prov_user_scope_hash "
                     "ON mem_provenance(user_id, scope, content_hash)")

    @staticmethod
    def _meta_has_scope(conn) -> bool:
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(mem_meta)").fetchall()}
        except Exception:
            return False
        return "scope" in cols

    # ---- reads ---------------------------------------------------------------

    def list_scopes(self, user_id) -> List[Dict]:
        """Every non-empty scope of *user_id*: ``{label, rows, oldest_ts, newest_ts}``,
        newest first. An owner read surface; never a recall path."""
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT scope, COUNT(*) AS n, MIN(ts) AS oldest, MAX(ts) AS newest "
                "FROM mem_provenance WHERE user_id = ? AND scope != '' "
                "GROUP BY scope ORDER BY newest DESC",
                (self._norm_user(user_id),), fetch="all") or []
        except Exception as e:
            logger.debug("list_scopes failed: %s", e)
            raise
        return [{"label": r["scope"], "rows": int(r["n"]), "oldest_ts": r["oldest"],
                 "newest_ts": r["newest"]} for r in rows]

    def scope_rows(self, user_id, label: str, *, limit: int = 20) -> List[Dict]:
        """The newest rows held under *label* (owner inspection)."""
        rows = execute_retry(
            self.db_path,
            "SELECT m.rowid AS rowid, m.content AS content, p.ts AS ts "
            "FROM mem_provenance p JOIN memories m ON m.rowid = p.mem_rowid "
            "WHERE p.user_id = ? AND p.scope = ? ORDER BY p.ts DESC LIMIT ?",
            (self._norm_user(user_id), str(label), max(1, min(int(limit), 200))),
            fetch="all") or []
        return [{"rowid": r["rowid"], "content": r["content"], "ts": r["ts"]} for r in rows]

    def count_scoped_rows(self) -> int:
        """Scoped rows across ALL tenants — the doctor's "flag OFF but quarantines
        exist" probe. 0 on any error."""
        try:
            row = execute_retry(self.db_path,
                                "SELECT COUNT(*) AS n FROM mem_provenance WHERE scope != ''",
                                (), fetch="one")
            return int(row["n"]) if row else 0
        except Exception:
            return 0

    # ---- promotion / purge ---------------------------------------------------

    def _promote_blocking(self, norm_user: str, label: str,
                          max_rows: int) -> Tuple[int, List[str]]:
        """ONE transaction: the keyword sidecar and its vector twin move together,
        so FTS never says shared while the vector store says scoped. With
        ``MEMORY_THREAT_SCAN`` on, a row that fails the scan is PURGED, not
        promoted. Idempotent: a second call matches zero rows. Returns
        ``(promoted, purged_contents)``."""
        scan = bool_env("MEMORY_THREAT_SCAN", False)
        conn = wal_connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT p.mem_rowid AS rowid, m.content AS content FROM mem_provenance p "
                "LEFT JOIN memories m ON m.rowid = p.mem_rowid "
                "WHERE p.user_id = ? AND p.scope = ? ORDER BY p.ts DESC LIMIT ?",
                (norm_user, label, max(1, int(max_rows)))).fetchall()
            keep, purge = [], []
            for r in rows:
                content = r[1] or ""
                if scan and content:
                    from modules.memory.task.threat_scan import is_suspicious
                    if is_suspicious(content):
                        purge.append((r[0], content))
                        continue
                keep.append((r[0], content))
            meta = self._meta_has_scope(conn)
            for rowid, content in keep:
                conn.execute("UPDATE mem_provenance SET scope = '' "
                             "WHERE mem_rowid = ? AND scope = ?", (rowid, label))
                if meta:
                    conn.execute("UPDATE mem_meta SET scope = '' WHERE user_id = ? "
                                 "AND scope = ? AND content = ?", (norm_user, label, content))
            for rowid, _content in purge:
                conn.execute("DELETE FROM memories WHERE rowid = ?", (rowid,))
                conn.execute("DELETE FROM mem_provenance WHERE mem_rowid = ?", (rowid,))
            conn.commit()
            return len(keep), [c for _r, c in purge]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def promote_scope(self, user_id, label: str, *, max_rows: Optional[int] = None) -> int:
        """Move the newest ``max_rows`` rows of *label* to the shared pool. Returns
        the count promoted (0 on error or nothing to do)."""
        from modules.memory.scope import PROMOTE_MAX_ROWS, valid_label
        if not valid_label(label):
            return 0
        try:
            promoted, purged = self._promote_blocking(
                self._norm_user(user_id), str(label), max_rows or PROMOTE_MAX_ROWS)
        except Exception as e:
            logger.warning("promote_scope failed: %s", e)
            return 0
        if purged:
            logger.warning("memory scope %s: %d row(s) failed the threat scan and were "
                           "purged instead of promoted", label, len(purged))
            self._scope_purged_hook(self._norm_user(user_id), purged, label=str(label))
        return promoted

    def purge_scope(self, user_id, label: str) -> int:
        """Delete every row held under *label* (FTS row + sidecars). Returns the count."""
        from modules.memory.scope import valid_label
        if not valid_label(label):
            return 0
        norm = self._norm_user(user_id)
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT p.mem_rowid AS rowid, m.content AS content FROM mem_provenance p "
                "LEFT JOIN memories m ON m.rowid = p.mem_rowid "
                "WHERE p.user_id = ? AND p.scope = ?", (norm, str(label)), fetch="all") or []
            self._delete_rowids([r["rowid"] for r in rows])
        except Exception as e:
            logger.warning("purge_scope failed: %s", e)
            return 0
        self._scope_purged_hook(norm, [r["content"] for r in rows if r["content"]],
                                label=str(label))
        return len(rows)

    def purge_stale_scopes(self, *, older_than_ts: int) -> int:
        """Curator retention: every scoped row (all tenants) older than the cutoff.
        A quarantine is working memory, not an archive — anything worth keeping
        was promoted."""
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT p.mem_rowid AS rowid, p.user_id AS user_id, p.scope AS scope, "
                "m.content AS content FROM mem_provenance p LEFT JOIN memories m ON m.rowid = p.mem_rowid "
                "WHERE p.scope != '' AND p.ts < ?", (int(older_than_ts),), fetch="all") or []
            self._delete_rowids([r["rowid"] for r in rows])
        except Exception as e:
            logger.warning("purge_stale_scopes failed: %s", e)
            return 0
        groups: Dict[Tuple[str, str], List[str]] = {}
        for r in rows:
            if r["content"]:
                groups.setdefault((r["user_id"], r["scope"]), []).append(r["content"])
        for (user, label), contents in groups.items():
            self._scope_purged_hook(user, contents, label=label)
        return len(rows)

    def _scope_purged_hook(self, norm_user: str, contents: List[str], *,
                           label: Optional[str] = None) -> None:
        """Drop the ``mem_meta`` twins held under *label* (a plain table, so the
        stdlib can reach it — the owner CLI opens the keyword store only). The
        vector provider overrides this to drop the ``mem_vec`` rows too."""
        if not label or not contents:
            return
        try:
            conn = wal_connect(self.db_path)
            try:
                if self._meta_has_scope(conn):
                    conn.executemany("DELETE FROM mem_meta WHERE user_id = ? AND scope = ? "
                                     "AND content = ?", [(norm_user, label, c) for c in contents])
                    conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.debug("mem_meta scope twin purge skipped: %s", e)

    def _delete_rowids(self, ids) -> None:
        for i in range(0, len(ids), self._PRUNE_BATCH):
            chunk = tuple(ids[i:i + self._PRUNE_BATCH])
            marks = ",".join("?" for _ in chunk)
            execute_retry(self.db_path, f"DELETE FROM memories WHERE rowid IN ({marks})", chunk)
            execute_retry(self.db_path,
                          f"DELETE FROM mem_provenance WHERE mem_rowid IN ({marks})", chunk)

    # ---- age retention (B3, moved from sqlite_memory_provider) -------------

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
            self._delete_rowids(ids)
            return len(ids)
        except Exception as e:
            logger.warning("prune_memories failed: %s", e)
            return 0


def scope_age_cutoff(now: Optional[float] = None) -> int:
    """The retention cutoff for :meth:`ScopeStoreMixin.purge_stale_scopes`."""
    from modules.memory.scope import RETENTION_DAYS
    return int((time.time() if now is None else now) - RETENTION_DAYS * 86400)
