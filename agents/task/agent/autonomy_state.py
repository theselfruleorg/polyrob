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

from core.security.forged_turns import FORGED_TURN_KINDS
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
                   delivered_at REAL,
                   PRIMARY KEY (session_id, delegation_id)
               )""",
        )
        execute_retry(
            db_path,
            "CREATE INDEX IF NOT EXISTS idx_delegations_status ON delegations(status)",
        )
        # T1.6: idempotent migration for a DB created before delivered_at existed
        # (mirrors goals/board.py's PRAGMA table_info guard).
        cols = execute_retry(db_path, "PRAGMA table_info(delegations)", fetch="all")
        col_names = {r["name"] for r in (cols or [])}
        if "delivered_at" not in col_names:
            execute_retry(db_path, "ALTER TABLE delegations ADD COLUMN delivered_at REAL")
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

    # Statuses whose result_text is a genuine finished-child payload worth
    # redelivering after a restart (T1.6). Deliberately excludes 'cancelled'
    # (the live path never populates a result for it — delivery is skipped by
    # design on cancellation, see async_delegation.py) and 'interrupted' (the
    # sibling running-delegation sweep pass already best-effort surfaced it, and
    # it has no real child result to redeliver).
    _TERMINAL_DELIVERABLE_STATUSES = ("completed", "error", "timeout")

    def list_completed_undelivered(self) -> List[dict]:
        """Terminal rows whose already-computed result was never surfaced.

        Covers the restart gap: the process died between ``record_terminal()``
        persisting the result and the caller's delivery succeeding. Idempotent
        by construction — once :meth:`mark_delivered` stamps a row it drops out
        of this query, so a repeated cold-start sweep redelivers nothing.
        """
        placeholders = ",".join("?" for _ in self._TERMINAL_DELIVERABLE_STATUSES)
        rows = execute_retry(
            self.db_path,
            f"SELECT * FROM delegations WHERE status IN ({placeholders}) "
            "AND delivered_at IS NULL",
            tuple(self._TERMINAL_DELIVERABLE_STATUSES),
            fetch="all",
        )
        return [dict(r) for r in (rows or [])]

    def mark_delivered(self, session_id: str, delegation_id: str,
                       delivered_at: float) -> int:
        """CAS-stamp ``delivered_at`` the instant a delegation result has
        ACTUALLY entered a session turn (T1.6 review fix). Returns the number
        of rows changed (0 or 1).

        The ONE call site is the drain path (:func:`stamp_delivered_from_drain`,
        invoked from ``agents/task/agent/core/user_ingress.py::
        _drain_user_messages``) — never at submit/park time. ``submit_user_message``
        /``deliver_self_wake`` only PARK a message in the in-memory HITL queue;
        "the call didn't raise" is not "delivered," and stamping there left an
        unbounded crash window where a row was marked delivered with the result
        still sitting, undrained, in memory (never surfaced, never recoverable —
        the exact failure mode this table exists to prevent).

        ``AND delivered_at IS NULL`` makes this a CAS: only the first caller for
        a given row actually changes it (returns 1); every later call is a
        no-op (returns 0). This matters because delivery is honestly
        at-least-once, not exactly-once — e.g. two racing cold starts
        (``workers>1`` / overlapping restarts) can both dispatch a self-wake for
        the same still-undelivered row before either drains, so the session may
        see a duplicate "[recovered after restart]" surfacing. That duplication
        is accepted (data loss is not); the CAS guarantees the row itself, and
        its delivered-once event, are stamped/emitted at most once regardless."""
        n = execute_retry(
            self.db_path,
            "UPDATE delegations SET delivered_at=? WHERE session_id=? AND "
            "delegation_id=? AND delivered_at IS NULL",
            (delivered_at, session_id, delegation_id),
        )
        return int(n or 0)

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


def stamp_delivered_from_drain(session_id: str, user_id: str, kind: str,
                               metadata: Optional[dict]) -> None:
    """T1.6 review fix — the ONE place ``delivered_at`` is stamped.

    Called from the drain path (``agents/task/agent/core/user_ingress.py::
    _drain_user_messages``) the instant a message is popped off the HITL queue
    and handed to ``inject_user_guidance`` — the point a delegation result has
    ACTUALLY entered the turn's message history, not merely been PARKED there
    by ``submit_user_message``/``deliver_self_wake``. This single site covers
    both producers of a redeliverable result, since both route through the
    same HITL ingress:

    * the live path — ``orchestrator._deliver_async_delegation`` (kind
      ``delegation_result``, metadata carries ``delegation_id``+``status``);
    * the cold-start completed-undelivered sweep (:func:`_sweep_completed_undelivered`)
      — ``TaskAgent.deliver_self_wake`` (kind ``self_wake``, metadata carries
      ``delegation_id``+``recovery``).

    Scoped to genuinely forged-kind messages (``FORGED_TURN_KINDS`` —
    self_wake/delegation_result): ``delegation_id`` presence in ``metadata``
    alone is NOT a safe trigger, because ``metadata`` on an ordinary
    "comment"/"guidance" message is caller-supplied over the public HTTP API
    (``api/task_http_api.py`` ``request.metadata``) — an arbitrary field-name
    collision must not be able to force a stamp on someone's own delegation row.

    ⚠️ The kind-gate alone raises the spoof bar but does NOT close it —
    ``kind`` is ALSO caller-controllable over that same public API (``POST
    /sessions/{sid}/messages`` has no server-side kind allowlist; the request
    model's ``kind``/``metadata`` fields are free-form). An owner who learns a
    ``delegation_id`` from their own dispatch result could set
    ``kind="delegation_result"`` and pre-stamp their OWN still-``running``
    delegation before it actually finishes. What actually closes the spoof is
    the TERMINAL-STATUS check below: a row is only eligible once its
    ``status`` is already one of :attr:`AutonomyStateStore.
    _TERMINAL_DELIVERABLE_STATUSES` (i.e. ``record_terminal`` has already run
    for it). Both legitimate producers — the live
    ``orchestrator._deliver_async_delegation`` path and the cold-start
    ``_sweep_completed_undelivered`` pass — only ever fire AFTER
    ``record_terminal``, so this is correct by construction and needs no
    call-site changes. A pre-stamp on a still-``running`` row is now a
    silent no-op: the row stays undelivered and is correctly re-surfaced once
    it genuinely completes.

    CAS via :meth:`AutonomyStateStore.mark_delivered` (``delivered_at IS
    NULL``): a duplicate drain (either copy of an at-least-once redelivered
    result, or a racing cold-start re-wake) stamps the row and emits the
    ``delegation_delivered`` event AT MOST ONCE.

    Fail-open throughout — durability off, no store, no delegation_id, no
    matching row, non-terminal row, or any store/event-log error is a silent
    no-op. Must never block message delivery.
    """
    if kind not in FORGED_TURN_KINDS or not metadata:
        return
    delegation_id = metadata.get("delegation_id")
    if not delegation_id:
        return
    try:
        store = get_autonomy_state_store()
        if store is None:
            return
        row = store.get(session_id, delegation_id)
        if not row or row.get("status") not in AutonomyStateStore._TERMINAL_DELIVERABLE_STATUSES:
            # Spoof guard: a caller-forged kind+delegation_id on a still-
            # running (or unknown) row is not yet a real result to deliver.
            return
        changed = store.mark_delivered(session_id, delegation_id, time.time())
    except Exception:
        logger.warning("delivered_at stamp failed for %s/%s (drain path)",
                       session_id, delegation_id, exc_info=True)
        return
    if not changed:
        return  # CAS: already stamped by an earlier drain — no duplicate event
    try:
        from core.event_log import get_event_log, event_log_enabled
        if event_log_enabled():
            get_event_log().record(
                "delegation_delivered", user_id=user_id or "", session_id=session_id,
                source="autonomy_state",
                attrs={"delegation_id": delegation_id, "status": row.get("status") or ""},
            )
    except Exception:
        pass


async def recover_interrupted_delegations(task_agent: Any, db_path: str) -> int:
    """Startup sweep over autonomy_state.db. Two passes, both cold-start-only:

    1. Rows still ``running`` at process start were crash-interrupted — mark
       ``interrupted`` and surface that (best-effort; the durable row remains
       the honest record when the wake is dropped/disabled). Never resumes
       the child.
    2. (T1.6) Rows already terminal (``completed``/``error``/``timeout``) whose
       result was persisted by ``record_terminal`` but never delivered before
       the process died — surface the ALREADY-COMPUTED result via the self-wake
       rail. Never re-runs the child. This pass does NOT stamp ``delivered_at``
       itself (review fix) — it only DISPATCHES the wake; the stamp happens in
       the drain path (:func:`stamp_delivered_from_drain`) the instant the
       parked message is actually consumed into a turn, so a crash between
       "wake dispatched" and "message drained" never falsely marks a row
       delivered. A row whose wake is never drained (session never runs again)
       legitimately stays undelivered and is re-surfaced on every subsequent
       cold start — the honest at-least-once retry contract, not a bug.

    Returns the total number of rows recovered across both passes. The
    self-wake delivery is best-effort throughout (SELF_WAKE_ENABLED off /
    non-resident session / budget exhausted → the wake is dropped, but the
    durable row remains the honest record).
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
            from core.event_log import get_event_log, event_log_enabled
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
    recovered += await _sweep_completed_undelivered(task_agent, store)
    return recovered


# Honest per-status framing for a redelivered result (T1.6). Never claims a
# delegation "completed" when it actually errored/timed out.
_STATUS_VERBS = {"completed": "completed", "error": "failed", "timeout": "timed out"}


async def _sweep_completed_undelivered(task_agent: Any, store: "AutonomyStateStore") -> int:
    """T1.6 second sweep pass: SURFACE (dispatch, don't stamp) already-persisted
    results the process died before delivering. Fail-open + per-item
    try/except, mirroring the interrupted-delegation pass above.

    Review fix: this pass no longer calls :meth:`AutonomyStateStore.mark_delivered`
    itself — it only dispatches ``deliver_self_wake``, which PARKS the result in
    the target session's HITL queue and (if idle) kicks a fresh run. The stamp
    happens later, in the drain path (:func:`stamp_delivered_from_drain`), the
    instant that parked message is actually consumed into a turn — so a crash
    between "wake dispatched" and "message drained" can never leave a row
    falsely marked delivered. A row whose wake never gets drained (e.g. the
    kicked run never reaches the drain, or self-wake is disabled entirely)
    legitimately stays undelivered and is re-surfaced on every subsequent cold
    start — logged, not capped; the honest at-least-once retry contract.

    Returns the number of rows successfully SURFACED (dispatch succeeded) this
    pass — NOT the number stamped delivered, which is no longer observable
    from here.
    """
    try:
        rows = store.list_completed_undelivered()
    except Exception:
        logger.warning("completed-undelivered delegation sweep failed to read store",
                       exc_info=True)
        return 0
    surfaced = 0
    for row in rows:
        session_id, delegation_id = row["session_id"], row["delegation_id"]
        status = row.get("status") or "completed"
        verb = _STATUS_VERBS.get(status, status)
        result_text = row.get("result_text") or ""
        text = (f"[recovered after restart] Background delegation {delegation_id} "
               f"{verb} while the process was down. Result:\n{result_text}")
        try:
            deliver = getattr(task_agent, "deliver_self_wake", None)
            if deliver is None:
                continue
            ok = await deliver(
                session_id, row.get("user_id", ""), text,
                metadata={"delegation_id": delegation_id, "status": status,
                          "recovery": "completed_undelivered_delegation"},
            )
        except Exception:
            logger.warning("completed-undelivered delegation wake for %s/%s failed "
                           "(row remains unstamped, retried next cold start)",
                           session_id, delegation_id, exc_info=True)
            continue
        if not ok:
            # SELF_WAKE disabled / budget exhausted / non-resident session:
            # deliver_self_wake returns False without raising — nothing was even
            # parked. Leave the row alone; retried next cold start.
            logger.info(
                "completed-undelivered delegation %s/%s not surfaced this cold "
                "start (self-wake unavailable) — will retry next cold start",
                session_id, delegation_id,
            )
            continue
        surfaced += 1
        logger.info(
            "completed-undelivered delegation %s/%s surfaced via self-wake "
            "(delivered_at stamps on drain, once the parked message actually "
            "enters a turn — not here)", session_id, delegation_id,
        )
    return surfaced
