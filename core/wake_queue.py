"""Durable cross-process session wake queue (043 W10).

When an owner approves a gated tool call from the WEB CONSOLE, the session that
asked for it must be re-entered so the one-shot grant is redeemed while the owner
is still there. In-process this rides the self-wake rail
(``tools/controller/approval_queue.py::decide_tool_approval`` with a live
``task_agent``). But on a split-service install the console (``polyrob-webview.service``) and
the agent (``polyrob.service``) run as SEPARATE processes, so the deciding
process holds NO ``task_agent`` for that session — and
``TaskAgent.deliver_self_wake`` refuses a remote session BY DESIGN (it only ever
wakes a session its OWN process is resident-with or can recreate). A console kick
straight into ``deliver_self_wake`` would either no-op or, worse, recreate the
session in the console process — the cross-worker hazard the ruling forbids.

This queue is the honest cross-process shape:

  * the DECIDING process ENQUEUES a durable wake row (survives a restart of
    either service; needs no new open port);
  * the OWNING process's autonomy tick (``polyrob/app/autonomy_runtime.py`` wake-drain
    ticker) CLAIMS the row via a single-winner CAS and delivers it IN-PROCESS
    through the same ``deliver_self_wake`` rail — where the session really lives.

Why a small table, not ``telemetry_events`` + a new kind: the event log is an
append-only AUDIT rail with no per-row consume/claim semantics. A wake must be
delivered EXACTLY once and marked, which needs a mutable ``status`` + a CAS —
the established durable-work-queue shape in this tree (``goals.db``, ``cron.db``,
``bridges.db``, all WAL + jittered retry via ``core.sqlite_util``). Registered in
``core/db_manifest.py`` so backup/rollback snapshot it.

Layering: core-tier, imports only stdlib + ``core.sqlite_util`` /
``core.runtime_paths``. The producer (tools) and drainer (core) both live
outside ``webview``, so nothing about this bypasses the ``blocks_goal_ids``
carve-out — the producer simply never enqueues a goal-blocking approval (that
redemption belongs to the re-armed goal + the dispatcher, per
``decide_tool_approval``).

Failure-mode: init/enqueue fail-open + LOUD (a broken queue must never break an
owner's approval — the ask row still flips regardless; a lost wake only costs the
resume-nicety, and the durable grant is still redeemed on the next identical
attempt). Reads in the drain fail-open too.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.runtime_paths import sidecar_db_path
from core.sqlite_util import execute_retry, init_schema, wal_connect

logger = logging.getLogger("core.wake_queue")

#: How often the owning process drains pending wakes.
WAKE_DRAIN_INTERVAL_SEC = 20.0
#: Delivery attempts before a wake is dropped as undeliverable (a session that is
#: gone, or a system paused for longer than this budget). The grant itself is
#: durable, so a dropped wake only loses the resume-nicety.
WAKE_MAX_ATTEMPTS = 5
#: A wake older than this is pruned unconditionally — it is well past the point a
#: "resume now" nudge is useful (the grant TTL is measured in hours).
WAKE_MAX_AGE_SEC = 6 * 60 * 60
#: A row CLAIMED but never resolved this long ago belonged to a drainer that
#: crashed mid-delivery — reclaim it so the wake is not stranded.
WAKE_STALE_CLAIM_SEC = 300

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_DELIVERED = "delivered"
STATUS_DROPPED = "dropped"

from core.state.machine import StateMachine  # noqa: E402

#: 098 S1 / CO-16: every exit from ``claimed`` is a CAS on the claimant.
WAKE_MACHINE = StateMachine(
    name="wake",
    states=frozenset({STATUS_PENDING, STATUS_CLAIMED, STATUS_DELIVERED, STATUS_DROPPED}),
    transitions={STATUS_PENDING: frozenset({STATUS_CLAIMED, STATUS_DROPPED}),
                 STATUS_CLAIMED: frozenset({STATUS_DELIVERED, STATUS_PENDING, STATUS_DROPPED})},
    terminal=frozenset({STATUS_DELIVERED, STATUS_DROPPED}),
)

#: Default wake row kind (the resume-on-grant nudge). Mirrors the ``metadata.kind``
#: the in-process path stamps, so both rails read the same on the agent side.
WAKE_KIND_APPROVAL = "approval_granted"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_wakes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'approval_granted',
    metadata    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    claimed_by  TEXT,
    claimed_at  REAL,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_sw_status ON session_wakes(status);
"""


#: TS-14: one live row per idempotency key (an approved outbound send is
#: enqueued by whichever of two writers sees "approved" — ONE row results).
_DEDUP_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_sw_dedup ON session_wakes(dedup_key)
    WHERE dedup_key IS NOT NULL;
"""


@dataclass
class WakeRow:
    id: int
    session_id: str
    user_id: str
    text: str
    kind: str
    metadata: Dict[str, Any]
    attempts: int
    created_at: float


class WakeQueue:
    """Durable session-wake queue. Init/writes fail-open + LOUD; reads fail-open."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ready = False
        try:
            init_schema(db_path, _SCHEMA, mkdir=True,
                        columns={"session_wakes": (("dedup_key", "TEXT"),)})
            init_schema(db_path, _DEDUP_INDEX)
            self._ready = True
        except Exception as e:
            logger.error(f"wake_queue init failed ({db_path}): {e}")

    # -- producer -------------------------------------------------------------

    def enqueue(self, session_id: str, user_id: str, text: str, *,
                kind: str = WAKE_KIND_APPROVAL,
                metadata: Optional[Dict[str, Any]] = None,
                dedup_key: Optional[str] = None) -> Optional[int]:
        """Write a durable pending wake for ``session_id``. Returns the row id, or
        None on failure (fail-open + LOUD — the caller's ask row already flipped).
        ``dedup_key``: a second enqueue with the same key writes nothing and
        returns the first row's id (098 TS-14)."""
        if not self._ready or not session_id or not text:
            return None
        try:
            blob = json.dumps(metadata or {}, default=str)
        except Exception:
            blob = "{}"
        try:
            self._prune_aged()
            if dedup_key:
                row = execute_retry(
                    self.db_path,
                    "INSERT INTO session_wakes (session_id, user_id, text, kind, metadata, "
                    "status, attempts, created_at, dedup_key) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?) "
                    "ON CONFLICT(dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING "
                    "RETURNING id",
                    (str(session_id), str(user_id or ""), str(text), str(kind), blob,
                     time.time(), str(dedup_key)), fetch="one")
                if row is None:
                    row = execute_retry(
                        self.db_path, "SELECT id FROM session_wakes WHERE dedup_key=?",
                        (str(dedup_key),), fetch="one")
                return int(row[0]) if row else None
            rowid = execute_retry(
                self.db_path,
                "INSERT INTO session_wakes "
                "(session_id, user_id, text, kind, metadata, status, attempts, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)",
                (str(session_id), str(user_id or ""), str(text), str(kind), blob,
                 time.time()),
                fetch="lastrowid",
            )
            return int(rowid) if rowid is not None else None
        except Exception as e:
            logger.error(f"wake_queue enqueue failed (session={session_id}): {e}")
            return None

    # -- drainer --------------------------------------------------------------

    def claim_pending(self, owner: str, *, limit: int = 20) -> List[WakeRow]:
        """Reclaim stale claims, prune aged rows, then atomically claim up to
        ``limit`` pending rows for ``owner`` (single-winner CAS per row). Returns
        the rows this call won. Fail-open to []."""
        if not self._ready:
            return []
        now = time.time()
        # 1) reclaim rows a crashed drainer left CLAIMED.
        self._exec(
            "UPDATE session_wakes SET status='pending', claimed_by=NULL, "
            "claimed_at=NULL WHERE status='claimed' AND claimed_at < ?",
            (now - WAKE_STALE_CLAIM_SEC,))
        # 2) prune rows past their useful life.
        self._prune_aged(now=now)
        # 3) pick candidates + CAS-claim each.
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT id FROM session_wakes WHERE status='pending' "
                "ORDER BY created_at LIMIT ?",
                (int(limit),), fetch="all") or []
        except Exception as e:
            logger.error(f"wake_queue claim scan failed: {e}")
            return []
        won: List[WakeRow] = []
        for r in rows:
            rid = r[0] if not isinstance(r, dict) else r["id"]
            try:
                changed = execute_retry(
                    self.db_path,
                    "UPDATE session_wakes SET status='claimed', claimed_by=?, "
                    "claimed_at=?, attempts=attempts+1 WHERE id=? AND status='pending'",
                    (str(owner), now, int(rid)))
            except Exception:
                changed = 0
            if not changed:
                continue  # another drainer won this row
            row = self._load(int(rid))
            if row is not None:
                won.append(row)
        return won

    @staticmethod
    def _claim_guard(owner: Optional[str]) -> tuple:
        if owner is None:
            return "status='claimed'", ()
        return "status='claimed' AND claimed_by=?", (str(owner),)

    def renew(self, wake_id: int, owner: str) -> bool:
        """CO-16: re-check (and refresh) this drainer's claim right before it
        delivers — a stalled claim another drainer reclaimed is not ours."""
        try:
            return execute_retry(
                self.db_path, "UPDATE session_wakes SET claimed_at=? "
                "WHERE id=? AND status='claimed' AND claimed_by=?",
                (time.time(), int(wake_id), str(owner))) == 1
        except Exception:
            return False

    def mark_delivered(self, wake_id: int, owner: Optional[str] = None) -> bool:
        """``claimed -> delivered`` for the claimant only (CO-16): a stale
        claimer whose row was reclaimed or aged out changes nothing."""
        guard, gp = self._claim_guard(owner)
        WAKE_MACHINE.check(STATUS_CLAIMED, STATUS_DELIVERED)
        return self._cas(
            f"UPDATE session_wakes SET status='delivered', reason='delivered' "
            f"WHERE id=? AND {guard}", (int(wake_id), *gp))

    def fail(self, wake_id: int, *, reason: str = "undeliverable",
             owner: Optional[str] = None) -> bool:
        """A claimed wake could not be delivered. Requeue for another attempt, or
        drop it once it has exhausted ``WAKE_MAX_ATTEMPTS`` — the grant is durable,
        so a dropped resume-nudge is safe. One guarded statement: a row that was
        pruned (dropped) or reclaimed meanwhile is never resurrected."""
        guard, gp = self._claim_guard(owner)
        return self._cas(
            "UPDATE session_wakes SET "
            "status=CASE WHEN attempts>=? THEN 'dropped' ELSE 'pending' END, "
            "reason=CASE WHEN attempts>=? THEN ? ELSE reason END, "
            f"claimed_by=NULL, claimed_at=NULL WHERE id=? AND {guard}",
            (WAKE_MAX_ATTEMPTS, WAKE_MAX_ATTEMPTS, str(reason), int(wake_id), *gp))

    def release(self, wake_id: int, owner: Optional[str] = None) -> bool:
        """Put a claimed wake back WITHOUT spending an attempt: the session is
        busy with a live owner turn, not unreachable (084). The row's age cap
        still bounds how long it can wait."""
        guard, gp = self._claim_guard(owner)
        return self._cas(
            "UPDATE session_wakes SET status='pending', claimed_by=NULL, claimed_at=NULL, "
            f"attempts=MAX(attempts-1, 0) WHERE id=? AND {guard}", (int(wake_id), *gp))

    def _cas(self, sql: str, params: tuple) -> bool:
        try:
            return execute_retry(self.db_path, sql, params) == 1
        except Exception as e:
            logger.error(f"wake_queue write failed: {e}")
            return False

    # -- test / observability -------------------------------------------------

    def pending_count(self) -> int:
        try:
            row = execute_retry(
                self.db_path,
                "SELECT COUNT(*) FROM session_wakes WHERE status='pending'",
                (), fetch="one")
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def get(self, wake_id: int) -> Optional[WakeRow]:
        return self._load(int(wake_id))

    def status_of(self, wake_id: int) -> Optional[str]:
        try:
            row = execute_retry(
                self.db_path, "SELECT status FROM session_wakes WHERE id=?",
                (int(wake_id),), fetch="one")
            return row[0] if row else None
        except Exception:
            return None

    # -- internals ------------------------------------------------------------

    def _prune_aged(self, *, now: Optional[float] = None) -> None:
        cutoff = (now or time.time()) - WAKE_MAX_AGE_SEC
        self._exec(
            "UPDATE session_wakes SET status='dropped', reason='stale' "
            "WHERE status IN ('pending','claimed') AND created_at < ?", (cutoff,))

    def _exec(self, sql: str, params: tuple) -> None:
        try:
            execute_retry(self.db_path, sql, params)
        except Exception as e:
            logger.error(f"wake_queue write failed: {e}")

    def _load(self, wake_id: int) -> Optional[WakeRow]:
        try:
            row = execute_retry(
                self.db_path,
                "SELECT id, session_id, user_id, text, kind, metadata, attempts, "
                "created_at FROM session_wakes WHERE id=?",
                (int(wake_id),), fetch="one")
        except Exception:
            return None
        if not row:
            return None
        try:
            meta = json.loads(row[5]) if row[5] else {}
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        return WakeRow(
            id=int(row[0]), session_id=row[1], user_id=row[2], text=row[3],
            kind=row[4], metadata=meta, attempts=int(row[6]), created_at=float(row[7]))


# --- process-wide singleton keyed by db path ------------------------------------
_INSTANCES: Dict[str, WakeQueue] = {}


def default_wake_queue_path() -> str:
    """The db_manifest axis (``<data_home>/wakes.db``). Callers that hold a data
    dir (the drain tick) or a board (the producer) pass an explicit sibling path
    instead, so tests are isolated by their tmp data home — no env seam needed."""
    return str(sidecar_db_path("wakes.db"))


def get_wake_queue(db_path: Optional[str] = None) -> WakeQueue:
    """Get/create the shared wake queue for ``db_path`` (default: the data-home
    axis / ``WAKE_QUEUE_PATH``)."""
    if db_path is None:
        db_path = default_wake_queue_path()
    inst = _INSTANCES.get(db_path)
    if inst is None:
        inst = WakeQueue(db_path)
        _INSTANCES[db_path] = inst
    return inst


__all__ = [
    "WAKE_DRAIN_INTERVAL_SEC",
    "WAKE_KIND_APPROVAL",
    "WakeQueue",
    "WakeRow",
    "default_wake_queue_path",
    "get_wake_queue",
]
