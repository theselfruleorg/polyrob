"""``deployed_apps`` registry (proposal §3.5): the durable, tenant-scoped record
of every app the ``hf_deploy`` tool has published/attempted.

SQLite via the shared WAL+jitter helper (``core/sqlite_util``, mirrors
``goals.db``/``cron.db``/``autonomy_state.db``) so it is safe under
``workers>1``. Primary key is ``(app_name, user_id)`` — tenant scoping is
structural, not a filter an author can forget to add.
"""
import os
import time
from typing import Any, Dict, List, Optional

from core.sqlite_util import execute_retry, wal_connect

_VALID_STATUSES = frozenset({"pending", "approved", "live", "failed", "undeployed"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployed_apps (
    app_name          TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    space_repo        TEXT,
    public_url        TEXT,
    health_path       TEXT,
    workspace_digest  TEXT,
    approved_at       REAL,
    last_deploy       REAL,
    last_failure_error TEXT,
    created_at        REAL NOT NULL,
    PRIMARY KEY (app_name, user_id)
);
CREATE INDEX IF NOT EXISTS idx_deployed_apps_user   ON deployed_apps(user_id);
CREATE INDEX IF NOT EXISTS idx_deployed_apps_status ON deployed_apps(status);

CREATE TABLE IF NOT EXISTS deploy_attempts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name  TEXT NOT NULL,
    user_id   TEXT NOT NULL,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deploy_attempts_user ON deploy_attempts(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_deploy_attempts_app  ON deploy_attempts(app_name, user_id, ts);
"""


def default_deployed_apps_db() -> str:
    """Resolve ``deployed_apps.db`` NEXT TO its sibling autonomy DBs.

    ``DEPLOYED_APPS_DB_PATH`` always wins (the seam the test suite uses to keep
    this DB out of the developer's real data home — see ``tests/conftest.py``'s
    autouse redirect). Otherwise mirrors ``autonomy_state.py::
    default_autonomy_state_db``: prefer the container config's ``data_dir``,
    fall back to ``get_data_root()``.
    """
    override = os.getenv("DEPLOYED_APPS_DB_PATH")
    if override:
        return override
    try:
        from core.container import DependencyContainer
        cfg = DependencyContainer.get_instance().get_service("config")
        data_dir = getattr(cfg, "data_dir", None)
        if data_dir:
            return os.path.join(str(data_dir), "deployed_apps.db")
    except Exception:
        pass
    from core.runtime_config import get_data_root
    return os.path.join(get_data_root(), "deployed_apps.db")


class DeployedAppsRegistry:
    """WAL-backed store of deployed-app rows + a per-tenant attempt ledger."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        conn = wal_connect(self.db_path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # --- reads -----------------------------------------------------------

    def get(self, app_name: str, user_id: str) -> Optional[Dict[str, Any]]:
        row = execute_retry(
            self.db_path,
            "SELECT * FROM deployed_apps WHERE app_name=? AND user_id=?",
            (app_name, user_id), fetch="one",
        )
        return dict(row) if row else None

    def list_for(self, user_id: str) -> List[Dict[str, Any]]:
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM deployed_apps WHERE user_id=? ORDER BY created_at",
            (user_id,), fetch="all",
        ) or []
        return [dict(r) for r in rows]

    def list_live_all(self) -> List[Dict[str, Any]]:
        """Every ``live`` row, across ALL tenants — the boot-reconcile feed."""
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM deployed_apps WHERE status='live'",
            (), fetch="all",
        ) or []
        return [dict(r) for r in rows]

    def deploys_in_last_day(self, user_id: str) -> int:
        since = time.time() - 86400
        row = execute_retry(
            self.db_path,
            "SELECT COUNT(*) AS n FROM deploy_attempts WHERE user_id=? AND ts > ?",
            (user_id, since), fetch="one",
        )
        if not row:
            return 0
        try:
            return int(row["n"])
        except (KeyError, TypeError, IndexError):
            return int(row[0])

    def last_attempt_epoch(self, app_name: str, user_id: str) -> Optional[float]:
        row = execute_retry(
            self.db_path,
            "SELECT MAX(ts) AS t FROM deploy_attempts WHERE app_name=? AND user_id=?",
            (app_name, user_id), fetch="one",
        )
        try:
            t = row["t"] if row else None
        except (KeyError, TypeError, IndexError):
            t = row[0] if row else None
        return float(t) if t is not None else None

    # --- mutations ---------------------------------------------------------

    def upsert_pending(self, app_name: str, user_id: str, *,
                       space_repo: Optional[str] = None) -> None:
        """Insert a fresh ``pending`` row. NEVER clobbers an existing row —
        an already-known app keeps its approval/space_repo/status untouched."""
        execute_retry(
            self.db_path,
            "INSERT OR IGNORE INTO deployed_apps (app_name,user_id,status,space_repo,created_at) "
            "VALUES (?,?,?,?,?)",
            (app_name, user_id, "pending", space_repo, time.time()),
        )

    def mark_approved(self, app_name: str, user_id: str) -> None:
        execute_retry(
            self.db_path,
            "UPDATE deployed_apps SET approved_at=? WHERE app_name=? AND user_id=?",
            (time.time(), app_name, user_id),
        )

    def record_live(self, app_name: str, user_id: str, *, space_repo: str,
                    public_url: str, health_path: str, workspace_digest: str) -> None:
        now = time.time()
        rc = execute_retry(
            self.db_path,
            "UPDATE deployed_apps SET status='live', space_repo=?, public_url=?, "
            "health_path=?, workspace_digest=?, last_deploy=? WHERE app_name=? AND user_id=?",
            (space_repo, public_url, health_path, workspace_digest, now, app_name, user_id),
        )
        if not rc:
            execute_retry(
                self.db_path,
                "INSERT INTO deployed_apps (app_name,user_id,status,space_repo,public_url,"
                "health_path,workspace_digest,created_at,last_deploy) VALUES (?,?,?,?,?,?,?,?,?)",
                (app_name, user_id, "live", space_repo, public_url, health_path,
                 workspace_digest, now, now),
            )

    def record_failed(self, app_name: str, user_id: str, *, error: Optional[str] = None) -> None:
        rc = execute_retry(
            self.db_path,
            "UPDATE deployed_apps SET status='failed', last_failure_error=? "
            "WHERE app_name=? AND user_id=?",
            (error, app_name, user_id),
        )
        if not rc:
            execute_retry(
                self.db_path,
                "INSERT INTO deployed_apps (app_name,user_id,status,last_failure_error,created_at) "
                "VALUES (?,?,?,?,?)",
                (app_name, user_id, "failed", error, time.time()),
            )

    def set_status(self, app_name: str, user_id: str, status: str) -> None:
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid status {status!r} (use {sorted(_VALID_STATUSES)})")
        execute_retry(
            self.db_path,
            "UPDATE deployed_apps SET status=? WHERE app_name=? AND user_id=?",
            (status, app_name, user_id),
        )

    def record_attempt(self, app_name: str, user_id: str) -> None:
        execute_retry(
            self.db_path,
            "INSERT INTO deploy_attempts (app_name,user_id,ts) VALUES (?,?,?)",
            (app_name, user_id, time.time()),
        )
