"""Restart-durable autonomy state.

Two registries used to be volatile process memory, making autonomy turn-durable
but not restart-durable:

- ``AsyncDelegationRegistry`` (UP-12) — a background delegation dispatched and
  then lost to a restart silently evaporated: the parent session was promised a
  ``delegation_result`` that never arrived.
- ``ReentryBudget`` (W1) — a forged-wake storm mid-ping-pong got a free depth
  reset by crashing.

``AutonomyStateStore`` persists both on the standard WAL+jitter sidecar pattern
(``core/sqlite_util``, same as goals.db/cron.db) in ``autonomy_state.db`` under
the data root (registered in ``core/db_manifest.py`` so backup/rollback snapshot
it). Recovery is HONEST, never magic: a delegation still ``running`` at process
start was crash-interrupted — :func:`recover_interrupted_delegations` marks it
``interrupted`` and surfaces that back to the originating session via the
self-wake rail (or leaves the durable row as the record when the wake is
dropped/disabled). The child coroutine itself is never resumed.

Everything here is fail-open: a store error degrades the registries to their
legacy in-memory behavior, never blocks a dispatch or a wake.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, List, Optional

from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)

_RESULT_TEXT_CAP = 4000
# Budget rows older than this are dropped on hydrate — a wake recorded days ago
# should not still be spacing/backing-off a fresh process.
STALE_BUDGET_SEC = 7 * 86400


def default_autonomy_state_db() -> str:
    """Resolve autonomy_state.db NEXT TO its sibling autonomy DBs.

    cron.db/goals.db/memory.db live under the container config's ``data_dir``
    (server: ``{POLYROB_DATA_DIR}/data``; CLI: reconciled to the ``.polyrob``
    root by build_cli_container) — prefer that, so ops tooling that assumes
    co-location never misses this DB. Fall back to ``get_data_root()`` (the CLI
    resolution) when no container config is available.
    """
    try:
        from core.container import DependencyContainer
        cfg = DependencyContainer.get_instance().get_service("config")
        data_dir = getattr(cfg, "data_dir", None)
        if data_dir:
            return os.path.join(str(data_dir), "autonomy_state.db")
    except Exception:
        pass
    from core.runtime_config import get_data_root
    return os.path.join(get_data_root(), "autonomy_state.db")


class AutonomyStateStore:
    """WAL-backed store for delegations + reentry budgets (one per data root)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        execute_retry(
            db_path,
            """CREATE TABLE IF NOT EXISTS delegations (
                   session_id TEXT NOT NULL,
                   delegation_id TEXT NOT NULL,
                   user_id TEXT NOT NULL DEFAULT '',
                   goal TEXT,
                   profile TEXT,
                   parent_agent_id TEXT,
                   status TEXT NOT NULL DEFAULT 'running',
                   dispatched_at REAL,
                   completed_at REAL,
                   result_text TEXT,
                   PRIMARY KEY (session_id, delegation_id)
               )""",
        )
        execute_retry(
            db_path,
            "CREATE INDEX IF NOT EXISTS idx_delegations_status ON delegations(status)",
        )
        execute_retry(
            db_path,
            """CREATE TABLE IF NOT EXISTS reentry_budget (
                   session_id TEXT PRIMARY KEY,
                   user_id TEXT NOT NULL DEFAULT '',
                   count INTEGER NOT NULL DEFAULT 0,
                   last_wake_at REAL NOT NULL DEFAULT 0
               )""",
        )

    # -- delegations -----------------------------------------------------------

    def record_dispatched(self, *, session_id: str, user_id: str, delegation_id: str,
                          goal: str, profile: str, parent_agent_id: Optional[str],
                          dispatched_at: float) -> None:
        execute_retry(
            self.db_path,
            """INSERT OR REPLACE INTO delegations
               (session_id, delegation_id, user_id, goal, profile, parent_agent_id,
                status, dispatched_at)
               VALUES (?,?,?,?,?,?,'running',?)""",
            (session_id, delegation_id, user_id, goal, profile,
             parent_agent_id, dispatched_at),
        )

    def record_terminal(self, session_id: str, delegation_id: str, *, status: str,
                        completed_at: float, result_text: str = "",
                        only_if_running: bool = False) -> int:
        """Record a delegation's terminal status. Returns the rows changed.

        ``only_if_running`` adds an ``AND status='running'`` CAS guard (P1
        finalization): the cold-start recovery sweep must NOT overwrite a
        delegation that a concurrent completion already moved to a genuine terminal
        state (completed/failed) between ``list_running()`` and this UPDATE — that
        would clobber a real result with a false 'interrupted'. The genuine
        completion path leaves this False (its write is authoritative)."""
        sql = ("UPDATE delegations SET status=?, completed_at=?, result_text=? "
               "WHERE session_id=? AND delegation_id=?")
        params = [status, completed_at, (result_text or "")[:_RESULT_TEXT_CAP],
                  session_id, delegation_id]
        if only_if_running:
            sql += " AND status='running'"
        n = execute_retry(self.db_path, sql, tuple(params))
        return int(n or 0)

    def get(self, session_id: str, delegation_id: str) -> Optional[dict]:
        row = execute_retry(
            self.db_path,
            "SELECT * FROM delegations WHERE session_id=? AND delegation_id=?",
            (session_id, delegation_id), fetch="one",
        )
        return dict(row) if row else None

    def list_running(self) -> List[dict]:
        rows = execute_retry(
            self.db_path, "SELECT * FROM delegations WHERE status='running'",
            fetch="all",
        )
        return [dict(r) for r in (rows or [])]

    def max_counter(self, session_id: str) -> int:
        """Highest numeric suffix of this session's deleg_NNNN ids — seeds the
        in-memory counter so a restarted session never reissues an id."""
        rows = execute_retry(
            self.db_path,
            "SELECT delegation_id FROM delegations WHERE session_id=?",
            (session_id,), fetch="all",
        )
        best = 0
        for r in rows or []:
            did = r["delegation_id"] or ""
            if did.startswith("deleg_"):
                try:
                    best = max(best, int(did.split("_", 1)[1]))
                except ValueError:
                    continue
        return best

    # -- reentry budget ----------------------------------------------------------

    def get_budget(self, session_id: str) -> Optional[dict]:
        row = execute_retry(
            self.db_path,
            "SELECT count, last_wake_at FROM reentry_budget WHERE session_id=?",
            (session_id,), fetch="one",
        )
        return dict(row) if row else None

    def put_budget(self, session_id: str, user_id: str, *, count: int,
                   last_wake_at: float) -> None:
        execute_retry(
            self.db_path,
            """INSERT INTO reentry_budget (session_id, user_id, count, last_wake_at)
               VALUES (?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                   count=excluded.count, last_wake_at=excluded.last_wake_at""",
            (session_id, user_id, count, last_wake_at),
        )

    def delete_budget(self, session_id: str) -> None:
        execute_retry(
            self.db_path, "DELETE FROM reentry_budget WHERE session_id=?",
            (session_id,),
        )


_STORE_LOCK = threading.Lock()
_STORE_CACHE: dict = {}


def get_autonomy_state_store() -> Optional[AutonomyStateStore]:
    """The store at the default path, or None when durability is off or the
    store cannot be opened (fail-open to legacy in-memory behavior).

    Memoized per resolved path — orchestrators are constructed per session and
    the store's schema init is blocking sqlite I/O; it must run once per
    process, not once per session.
    """
    try:
        from agents.task.constants import AutonomyConfig
        if not AutonomyConfig.autonomy_state_durable():
            return None
        path = default_autonomy_state_db()
        with _STORE_LOCK:
            store = _STORE_CACHE.get(path)
            if store is None:
                store = AutonomyStateStore(path)
                _STORE_CACHE[path] = store
            return store
    except Exception:
        logger.warning("autonomy_state store unavailable — running in-memory only",
                       exc_info=True)
        return None


def reset_autonomy_state_store_cache() -> None:
    """Test seam: drop memoized stores so path/env changes take effect."""
    with _STORE_LOCK:
        _STORE_CACHE.clear()


async def recover_interrupted_delegations(task_agent: Any, db_path: str) -> int:
    """Startup sweep: mark crash-interrupted delegations and surface them.

    Returns the number of rows recovered. The self-wake delivery is best-effort
    (SELF_WAKE_ENABLED off / non-resident session / budget exhausted → the wake
    is dropped, but the durable ``interrupted`` row remains the honest record).
    """
    if not os.path.exists(db_path):
        return 0
    try:
        store = AutonomyStateStore(db_path)
        rows = store.list_running()
    except Exception:
        logger.warning("delegation recovery sweep failed to read store", exc_info=True)
        return 0
    recovered = 0
    for row in rows:
        session_id, delegation_id = row["session_id"], row["delegation_id"]
        try:
            changed = store.record_terminal(
                session_id, delegation_id, status="interrupted",
                completed_at=time.time(),
                result_text="Process restarted while this delegation was running.",
                only_if_running=True,  # CAS: don't clobber a concurrent completion
            )
            if not changed:
                # The delegation reached a genuine terminal state between the
                # list_running() read and now — not actually interrupted. Skip the
                # false 'interrupted' event + wake.
                continue
            recovered += 1
        except Exception:
            logger.warning("could not mark delegation %s/%s interrupted",
                           session_id, delegation_id, exc_info=True)
            continue
        try:
            from agents.task.telemetry.event_log import get_event_log, event_log_enabled
            if event_log_enabled():
                get_event_log().record(
                    "delegation_interrupted", user_id=row.get("user_id", ""),
                    session_id=session_id, source="autonomy_state",
                    attrs={"delegation_id": delegation_id,
                           "goal": (row.get("goal") or "")[:200]},
                )
        except Exception:
            pass
        try:
            deliver = getattr(task_agent, "deliver_self_wake", None)
            if deliver is not None:
                await deliver(
                    session_id, row.get("user_id", ""),
                    (f"Background delegation {delegation_id} "
                     f"({(row.get('goal') or '')[:200]}) was interrupted by a process "
                     "restart and did NOT complete. Re-dispatch it if the work is "
                     "still needed."),
                    metadata={"delegation_id": delegation_id,
                              "recovery": "interrupted_delegation"},
                )
        except Exception:
            logger.warning("interrupted-delegation wake for %s failed (row remains "
                           "the durable record)", session_id, exc_info=True)
    return recovered
