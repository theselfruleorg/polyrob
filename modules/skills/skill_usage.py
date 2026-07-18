"""Skill provenance + usage metrics (W2-D).

Answers the question the safety brief requires of writable skills: *do authored
skills actually get reused?* Two tiny tenant-scoped tables in
``<data_dir>/skill_usage.db`` (shared WAL+jitter helpers):

- ``skill_provenance`` — who authored a skill and when (``user``/``agent``/
  ``background_review``);
- ``skill_usage`` — per-(skill,user) load_count + last_used_at, bumped each time the
  agent pulls a skill body via ``load_skill``.

Feeds W5 (curator: archive unused authored skills) and W7 (`/insights`: authored-skill
reuse %). Pure storage; fail-open at the call sites (a metrics write must never break
a skill load).
"""
from __future__ import annotations

import os
import time
import threading
from typing import Callable, Dict, List, Optional

from core.identity import is_anonymous
from core.sqlite_util import execute_retry, wal_connect


class SkillUsageStore:
    def __init__(self, db_path: str, *, clock: Callable[[], float] = time.time):
        self.db_path = db_path
        self._now = clock
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        conn = wal_connect(self.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS skill_provenance (
                    skill_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (skill_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS skill_usage (
                    skill_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    load_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at REAL,
                    PRIMARY KEY (skill_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS curator_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS skill_install_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    resolved_sha TEXT,
                    approver TEXT NOT NULL,
                    ts REAL NOT NULL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    # --- provenance ----------------------------------------------------------

    def record_provenance(self, skill_id: str, user_id: str, created_by: str) -> None:
        if is_anonymous(user_id):
            return
        execute_retry(
            self.db_path,
            """INSERT INTO skill_provenance (skill_id,user_id,created_by,created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(skill_id,user_id) DO UPDATE SET created_by=excluded.created_by""",
            (skill_id, user_id, created_by, self._now()),
        )

    def get_provenance(self, skill_id: str, user_id: str) -> Optional[Dict]:
        row = execute_retry(
            self.db_path,
            "SELECT * FROM skill_provenance WHERE skill_id=? AND user_id=?",
            (skill_id, user_id), fetch="one",
        )
        return dict(row) if row else None

    # --- usage ---------------------------------------------------------------

    def bump_load(self, skill_id: str, user_id: str) -> None:
        if is_anonymous(user_id):
            return
        execute_retry(
            self.db_path,
            """INSERT INTO skill_usage (skill_id,user_id,load_count,last_used_at)
               VALUES (?,?,1,?)
               ON CONFLICT(skill_id,user_id)
               DO UPDATE SET load_count=load_count+1, last_used_at=excluded.last_used_at""",
            (skill_id, user_id, self._now()),
        )

    def get_usage(self, skill_id: str, user_id: str) -> Dict:
        row = execute_retry(
            self.db_path,
            "SELECT * FROM skill_usage WHERE skill_id=? AND user_id=?",
            (skill_id, user_id), fetch="one",
        )
        return dict(row) if row else {"skill_id": skill_id, "user_id": user_id,
                                      "load_count": 0, "last_used_at": None}

    def list_authored(self, user_id: Optional[str] = None,
                      created_by: Optional[List[str]] = None) -> List[Dict]:
        """Join provenance+usage for authored skills (curator/insights feed)."""
        sql = ("SELECT p.skill_id, p.user_id, p.created_by, p.created_at, "
               "COALESCE(u.load_count,0) AS load_count, u.last_used_at "
               "FROM skill_provenance p LEFT JOIN skill_usage u "
               "ON p.skill_id=u.skill_id AND p.user_id=u.user_id")
        clauses, params = [], []
        if user_id is not None:
            clauses.append("p.user_id=?"); params.append(user_id)
        if created_by:
            clauses.append("p.created_by IN (%s)" % ",".join("?" * len(created_by)))
            params.extend(created_by)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        return [dict(r) for r in rows]

    def authored_reuse_summary(self, user_id: Optional[str] = None) -> Dict:
        """W7 /insights: did self-authored skills get reused?

        Returns counts + reuse rate over agent/background-authored skills — the
        measurement the writable-skills safety brief requires. Tenant-scoped when
        user_id is given.
        """
        rows = self.list_authored(user_id=user_id, created_by=["agent", "background_review"])
        total = len(rows)
        reused = sum(1 for r in rows if (r.get("load_count") or 0) > 0)
        by_author: Dict[str, int] = {}
        for r in rows:
            by_author[r["created_by"]] = by_author.get(r["created_by"], 0) + 1
        return {
            "authored_total": total,
            "authored_reused": reused,
            "reuse_rate": (reused / total) if total else 0.0,
            "by_author": by_author,
            "top": sorted(
                ({"skill_id": r["skill_id"], "loads": r.get("load_count") or 0} for r in rows),
                key=lambda d: d["loads"], reverse=True,
            )[:10],
        }

    # --- curator state -------------------------------------------------------

    def get_state(self, key: str) -> Optional[str]:
        row = execute_retry(self.db_path, "SELECT value FROM curator_state WHERE key=?",
                            (key,), fetch="one")
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        execute_retry(
            self.db_path,
            "INSERT INTO curator_state (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # --- install audit ---------------------------------------------------------

    def record_install(self, name: str, *, user_id: str, source: str,
                       resolved_sha: Optional[str] = None, approver: str,
                       ts: Optional[float] = None) -> None:
        """Append-only audit trail for `polyrob skill install`/`approve`: what
        skill, whose tenant, where it came from (source + resolved commit SHA
        when known), who approved it, and when. Never updates/dedupes — a
        re-install/re-approve of the same name is a NEW event worth keeping."""
        ts = self._now() if ts is None else ts
        execute_retry(
            self.db_path,
            """INSERT INTO skill_install_audit
                   (name,user_id,source,resolved_sha,approver,ts)
               VALUES (?,?,?,?,?,?)""",
            (name, user_id, source, resolved_sha, approver, ts),
        )

    def list_installs(self, user_id: Optional[str] = None) -> List[Dict]:
        sql = "SELECT name,user_id,source,resolved_sha,approver,ts FROM skill_install_audit"
        params: tuple = ()
        if user_id is not None:
            sql += " WHERE user_id=?"
            params = (user_id,)
        sql += " ORDER BY ts DESC"
        rows = execute_retry(self.db_path, sql, params, fetch="all") or []
        return [dict(r) for r in rows]


# --- process-wide singleton (shares one db with the curator) -----------------

_STORE_LOCK = threading.Lock()
_STORE: Optional[SkillUsageStore] = None


def _default_data_dir() -> str:
    """Stable ABSOLUTE data dir so the singleton resolves the same file regardless of
    cwd. Otherwise the relative ``"data"`` default (used by the load_skill/provenance
    call sites) could resolve to a different directory than the curator's
    ``config.data_dir`` (absolute), silently splitting reuse counts from the curator
    and /insights.

    WS-3 (2026-07-16): routes through the data-home SSOT (``resolve_data_home()`` —
    ``POLYROB_DATA_DIR`` else ``cwd/.polyrob``) instead of anchoring to the repo/install
    tree, so the usage DB lands with the other sidecar DBs (goals.db/cron.db/…) and the
    curator/insights read the same file. The explicit ``POLYROB_DATA_DIR`` short-circuit
    is kept for byte-identical behaviour when it is set.
    """
    env = os.getenv("POLYROB_DATA_DIR")
    if env:
        return os.path.abspath(env)
    from core.runtime_paths import resolve_data_home
    return str(resolve_data_home())


def get_skill_usage_store(data_dir: Optional[str] = None) -> SkillUsageStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            dd = os.path.abspath(data_dir) if data_dir else _default_data_dir()
            _STORE = SkillUsageStore(os.path.join(dd, "skill_usage.db"))
        elif data_dir:
            want = os.path.join(os.path.abspath(data_dir), "skill_usage.db")
            if want != _STORE.db_path:
                # First caller's path wins (singleton). Warn loudly if a later caller
                # (e.g. the curator) expected a different db — that would desync metrics.
                import logging
                logging.getLogger(__name__).warning(
                    "skill_usage store already bound to %s; ignoring requested %s "
                    "(metrics/curator may read a different file than intended)",
                    _STORE.db_path, want,
                )
        return _STORE


def reset_skill_usage_store() -> None:
    """Test seam."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None
