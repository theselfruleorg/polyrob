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

from core.sqlite_util import execute_retry, init_schema, wal_connect
from core.state.machine import StateMachine
from core.state.sql import cas_transition

#: 098 TS-30. ``deploying`` is written before the Space is published; a health
#: failure AFTER a publish is ``unhealthy`` (the Space exists), ``failed`` means
#: the publish itself failed. Every status write is a CAS on the state read.
HF_DEPLOY = StateMachine(
    "hf.deploy",
    states=frozenset({"pending", "deploying", "live", "unhealthy", "failed", "undeployed"}),
    transitions={
        "pending": frozenset({"deploying", "undeployed"}),
        # deploying -> deploying: a stale attempt (a crash mid-deploy) is retaken.
        "deploying": frozenset({"deploying", "live", "unhealthy", "failed", "undeployed"}),
        "live": frozenset({"unhealthy", "deploying", "undeployed"}),
        "unhealthy": frozenset({"live", "deploying", "undeployed"}),
        "failed": frozenset({"deploying", "undeployed"}),
        "undeployed": frozenset({"deploying"}),
    },
    aliases={"approved": "pending"},
    initial=frozenset({"pending", "deploying"}),
)

_VALID_STATUSES = frozenset(HF_DEPLOY.states)

#: A ``deploying`` row older than this is a crashed attempt: a new deploy may
#: retake it and the reconcile probes it.
STALE_DEPLOYING_SEC = 30 * 60

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
    from core.runtime_paths import data_home_db_path
    return data_home_db_path("deployed_apps.db", env_key="DEPLOYED_APPS_DB_PATH",
                             prefer_container=True)


class DeployedAppsRegistry:
    """WAL-backed store of deployed-app rows + a per-tenant attempt ledger."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        init_schema(self.db_path, _SCHEMA,
                    columns={"deployed_apps": (("deploy_started_at", "REAL"),)})

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

    def list_reconcilable_all(self, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Every row the boot reconcile re-checks, across ALL tenants: ``live``,
        ``unhealthy`` and a ``deploying`` row older than ``STALE_DEPLOYING_SEC``."""
        cutoff = (now if now is not None else time.time()) - STALE_DEPLOYING_SEC
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM deployed_apps WHERE status IN ('live','unhealthy') "
            "OR (status='deploying' AND COALESCE(deploy_started_at,0) < ?)",
            (cutoff,), fetch="all",
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

    def _cas(self, app_name: str, user_id: str, to: str, *, from_states=None,
             set_cols=None, where_extra: str = "", where_params: tuple = ()) -> bool:
        return cas_transition(
            self.db_path, "deployed_apps", key="app_name=? AND user_id=?",
            key_params=(app_name, user_id), machine=HF_DEPLOY, to=to,
            from_states=from_states, set_cols=set_cols or {},
            where_extra=where_extra, where_params=where_params)

    def begin_deploy(self, app_name: str, user_id: str, *,
                     space_repo: Optional[str] = None,
                     now: Optional[float] = None) -> bool:
        """Enter ``deploying`` BEFORE the Space is published. False when another
        deploy of this app is in progress (a ``deploying`` row younger than
        ``STALE_DEPLOYING_SEC``)."""
        now = now if now is not None else time.time()
        execute_retry(
            self.db_path,
            "INSERT OR IGNORE INTO deployed_apps (app_name,user_id,status,space_repo,created_at) "
            "VALUES (?,?,?,?,?)",
            (app_name, user_id, "pending", space_repo, now),
        )
        cols = {"deploy_started_at": now}
        if space_repo:
            cols["space_repo"] = space_repo
        return self._cas(app_name, user_id, "deploying", set_cols=cols,
                         where_extra="status != 'deploying' OR COALESCE(deploy_started_at,0) < ?",
                         where_params=(now - STALE_DEPLOYING_SEC,))

    def record_live(self, app_name: str, user_id: str, *, space_repo: str,
                    public_url: str, health_path: str, workspace_digest: str,
                    from_states=("deploying",), where_extra: str = "",
                    where_params: tuple = ()) -> bool:
        """``deploying -> live`` (or the reconcile's ``unhealthy -> live``)."""
        return self._cas(app_name, user_id, "live", from_states=from_states, set_cols={
            "space_repo": space_repo, "public_url": public_url, "health_path": health_path,
            "workspace_digest": workspace_digest, "last_deploy": time.time()},
            where_extra=where_extra, where_params=where_params)

    def record_unhealthy(self, app_name: str, user_id: str, *, error: Optional[str] = None,
                         from_states=("deploying",), where_extra: str = "",
                         where_params: tuple = (), set_cols=None) -> bool:
        """The Space was published but does not answer 2xx."""
        cols = {"last_failure_error": error}
        cols.update(set_cols or {})
        return self._cas(app_name, user_id, "unhealthy", from_states=from_states,
                         set_cols=cols, where_extra=where_extra, where_params=where_params)

    def record_failed(self, app_name: str, user_id: str, *, error: Optional[str] = None,
                      from_states=("deploying",), where_extra: str = "",
                      where_params: tuple = ()) -> bool:
        """The publish itself failed (no Space was put up by this attempt)."""
        return self._cas(app_name, user_id, "failed", from_states=from_states,
                         set_cols={"last_failure_error": error},
                         where_extra=where_extra, where_params=where_params)

    def set_status(self, app_name: str, user_id: str, status: str) -> bool:
        """Owner/tool edge to *status* from every state the table allows."""
        if HF_DEPLOY.normalize(status) not in _VALID_STATUSES:
            raise ValueError(f"invalid status {status!r} (use {sorted(_VALID_STATUSES)})")
        return self._cas(app_name, user_id, status)

    def record_attempt(self, app_name: str, user_id: str) -> None:
        execute_retry(
            self.db_path,
            "INSERT INTO deploy_attempts (app_name,user_id,ts) VALUES (?,?,?)",
            (app_name, user_id, time.time()),
        )
