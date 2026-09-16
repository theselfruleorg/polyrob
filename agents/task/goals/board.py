"""Durable goal board (W4, Reference-parity kanban_db).

A cross-session, durable backlog of agent-pursued goals — the thing POLYROB lacked
(it had only a session-scoped TODO). A goal outlives the turn that created it: a
dispatcher claims ``ready`` goals, runs them on the task-agent core, and records
success/failure with a circuit breaker. Completions feed the W1 self-wake rail so
a finished goal can forge a follow-up turn.

Storage is SQLite under ``<data_dir>/goals.db`` via the shared WAL+jitter helpers
(``core/sqlite_util``) — never a hand-rolled retry loop. Every query is
tenant-scoped (``AND user_id = ?``); claims are an atomic compare-and-set so the
board is safe under ``UVICORN_WORKERS>1`` + the agent-facing ``goal`` tool racing
the dispatcher.

Gated by ``GOALS_ENABLED`` at the call sites; this module is pure storage and is
inert until a dispatcher/tool touches it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)

# status lifecycle: triage -> waiting -> ready -> running -> {done | blocked} ; cancelled
# is terminal. `waiting` (T2.1) is a goal created with unresolved `depends_on` edges —
# it is NOT dispatchable (ready()'s filter excludes it with zero query changes) and
# flips to `ready` only when every prerequisite lands `done` (see deps_satisfied /
# the record_success completion sweep).
STATUS_TRIAGE = "triage"
STATUS_WAITING = "waiting"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

#: How many ready rows one fair-dispatch pass considers. The dispatcher asks for
#: 2-4 slots against a board of a few hundred rows, so scan a bounded window
#: rather than the table. A stream whose goals all sit below this window is one
#: whose neighbours are already saturated — the next tick sees it.
READY_SCAN_LIMIT = 200

# kind: rows are goals (dispatchable), objectives (standing, never dispatched),
# or asks (owner-facing needs, never dispatched)
KIND_GOAL = "goal"
KIND_OBJECTIVE = "objective"
KIND_ASK = "ask"

# ask lifecycle (§7.2b) — disjoint from goal statuses so nothing dispatches them
ASK_OPEN = "open"
ASK_FULFILLED = "fulfilled"
# Task 9 (G-2): a tool_approval ask's owner-declined outcome. Disjoint from the
# goal-status STATUS_CANCELLED string on purpose — an ask is never a goal.
ASK_REJECTED = "rejected"
# 2026-08-18: the ask answered itself — every goal it blocked reached a terminal
# state, so the owner no longer has a decision to make. Distinct from FULFILLED
# on purpose: "fulfilled" claims the owner acted, and an ask that expired because
# the work resolved itself must never be recorded as an owner decision. Prod held
# 44 open asks (oldest a month) with several naming goals that had since
# succeeded — that queue is what the owner has to read.
ASK_OBSOLETE = "obsolete"

# objective lifecycle (disjoint from goal statuses so nothing dispatches them)
OBJ_ACTIVE = "active"
OBJ_PAUSED = "paused"
OBJ_DROPPED = "dropped"
OBJ_DONE = "done"
_OBJECTIVE_STATUSES = {OBJ_ACTIVE, OBJ_PAUSED, OBJ_DROPPED, OBJ_DONE}


class DuplicateGoalError(ValueError):
    """A new goal's title is a near-duplicate of a recent goal."""

    def __init__(self, match_id: str, match_title: str, similarity: float):
        self.match_id = match_id
        self.match_title = match_title
        self.similarity = similarity
        super().__init__(
            f"near-duplicate of goal {match_id} '{match_title}' (similarity {similarity:.2f})")


def normalize_title(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())


def _trigrams(s: str) -> set:
    padded = f"  {s} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def title_similarity(a: str, b: str) -> float:
    ta, tb = _trigrams(normalize_title(a)), _trigrams(normalize_title(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class Goal:
    id: str
    user_id: str
    title: str
    body: str = ""
    kind: str = KIND_GOAL
    status: str = STATUS_READY
    priority: int = 5
    parent_id: Optional[str] = None
    claim_lock: Optional[str] = None
    claim_expires: Optional[float] = None
    consecutive_failures: int = 0
    max_retries: int = 2
    last_failure_error: Optional[str] = None
    session_id: Optional[str] = None
    result: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    last_heartbeat_at: Optional[float] = None

    @classmethod
    def from_row(cls, row) -> "Goal":
        d = dict(row)
        d["payload"] = json.loads(d.get("payload") or "{}")
        return cls(**d)


class GoalBoard:
    """SQLite-backed durable goal store with atomic claim + circuit breaker."""

    PLANNER_SENTINEL = "__planner__"

    def __init__(self, db_path: str, *, clock: Callable[[], float] = time.time,
                 id_factory: Optional[Callable[[], str]] = None):
        self.db_path = db_path
        self._now = clock
        self._id = id_factory or (lambda: uuid.uuid4().hex[:12])
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        conn = wal_connect(self.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS goals (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'goal',
                    status TEXT NOT NULL DEFAULT 'ready',
                    priority INTEGER NOT NULL DEFAULT 5,
                    parent_id TEXT,
                    claim_lock TEXT,
                    claim_expires REAL,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 2,
                    last_failure_error TEXT,
                    session_id TEXT,
                    result TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    last_heartbeat_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_goals_ready
                    ON goals(status, priority DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_goals_user ON goals(user_id);
                CREATE TABLE IF NOT EXISTS goal_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS goal_edges (
                    goal_id TEXT NOT NULL,
                    depends_on_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (goal_id, depends_on_id)
                );
                CREATE INDEX IF NOT EXISTS idx_goal_edges_dep ON goal_edges(depends_on_id);
                """
            )
            # Idempotent migration: add kind column if it doesn't exist
            cols = {r[1] for r in conn.execute("PRAGMA table_info(goals)").fetchall()}
            if "kind" not in cols:
                conn.execute("ALTER TABLE goals ADD COLUMN kind TEXT NOT NULL DEFAULT 'goal'")
            conn.commit()
        finally:
            conn.close()

    # --- mutations -----------------------------------------------------------

    def create(self, *, user_id: str, title: str, body: str = "", priority: int = 5,
               parent_id: Optional[str] = None, max_retries: Optional[int] = None,
               payload: Optional[Dict[str, Any]] = None, status: str = STATUS_READY,
               kind: str = KIND_GOAL, force: bool = False,
               depends_on: Optional[List[str]] = None) -> Goal:
        from core.identity import is_anonymous
        if is_anonymous(user_id):
            raise ValueError("goal create requires a real (non-anonymous) user_id (tenant scope)")
        # An objective is allowed to be standing; it is not allowed to be infinite.
        if kind == KIND_GOAL:
            self._check_objective_budget(user_id, parent_id)
        from agents.task.constants import AutonomyConfig

        # Check for near-duplicates in the last 7 days
        threshold = AutonomyConfig.goal_dedup_threshold()
        if not force and threshold > 0:
            since = self._now() - 7 * 86400
            rows = execute_retry(
                self.db_path,
                """SELECT id, title FROM goals
                    WHERE user_id=? AND created_at > ?
                      AND status NOT IN ('cancelled','dropped')
                      AND kind != 'ask'
                      AND (? IS NULL OR id != ?)""",
                (user_id, since, parent_id, parent_id), fetch="all",
            ) or []
            for r in rows:
                sim = title_similarity(title, r["title"])
                if sim >= threshold:
                    self._event(r["id"], "dedup_rejected",
                                {"attempted_title": title[:200], "similarity": round(sim, 3)})
                    raise DuplicateGoalError(r["id"], r["title"], sim)

        # --- DAG (T2.1 Task 1): resolve depends_on BEFORE the row is written, so an
        # invalid dep (unknown id / cross-tenant) never leaves an orphan goal row —
        # validation is all-or-nothing. All requested deps already done (or none
        # requested) is byte-identical to legacy create: no edges, no extra event,
        # no status change. A mix of done + open deps still writes the FULL edge
        # set (including the already-done ones) so later "waiting on"/"blocks"
        # listings are complete; deps_satisfied treats a done dep as
        # always-satisfied via a live join, never a snapshot.
        dep_ids = list(dict.fromkeys(depends_on or []))
        unresolved_ids: List[str] = []
        terminal_bad_ids: List[str] = []
        if dep_ids:
            deps = self._validate_dep_ids(dep_ids, user_id=user_id)
            unresolved_ids = [d.id for d in deps if d.status != STATUS_DONE]
            # T2.1 Task 2 (creation-time closure): a dep that is already CANCELLED
            # or BLOCKED-with-exhausted-retries (the breaker actually tripped —
            # `consecutive_failures >= max_retries`, distinguishing it from an
            # agent-declared `block_from_ready` block that may not have exhausted
            # retries) can never reach 'done'. Without this check the new goal
            # would sit in 'waiting' forever with a dead prerequisite (flagged at
            # the end of Task 1). Never reject the create — the goal is preserved,
            # just immediately 'blocked' so the owner can see + unblock it.
            terminal_bad_ids = [
                d.id for d in deps
                if d.status == STATUS_CANCELLED
                or (d.status == STATUS_BLOCKED and d.consecutive_failures >= d.max_retries)
            ]

        if terminal_bad_ids:
            effective_status = STATUS_BLOCKED
        elif unresolved_ids:
            effective_status = STATUS_WAITING
        else:
            effective_status = status

        effective_payload = dict(payload or {})
        if terminal_bad_ids:
            effective_payload["block_kind"] = "dep_failed"

        g = Goal(
            id=self._id(), user_id=user_id, title=title, body=body, kind=kind,
            status=effective_status,
            priority=priority, parent_id=parent_id,
            max_retries=AutonomyConfig.goal_max_retries() if max_retries is None else max_retries,
            payload=effective_payload, created_at=self._now(),
        )
        execute_retry(
            self.db_path,
            """INSERT INTO goals (id,user_id,title,body,kind,status,priority,parent_id,
                 consecutive_failures,max_retries,payload,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (g.id, g.user_id, g.title, g.body, g.kind, g.status, g.priority, g.parent_id,
             0, g.max_retries, json.dumps(g.payload), g.created_at),
        )
        self._event(g.id, "created", {"title": title})

        if unresolved_ids:
            now = self._now()
            for dep_id in dep_ids:
                execute_retry(
                    self.db_path,
                    """INSERT OR IGNORE INTO goal_edges
                            (goal_id, depends_on_id, user_id, created_at)
                        VALUES (?, ?, ?, ?)""",
                    (g.id, dep_id, user_id, now),
                )
            if terminal_bad_ids:
                self._event(g.id, "dep_failed", {"prerequisites": terminal_bad_ids})
            else:
                self._event(g.id, "waiting_on_deps", {"deps": unresolved_ids})

        return g

    def claim(self, goal_id: str, worker: str, *, ttl_seconds: int) -> Optional[Goal]:
        """Atomically transition a single ready goal to running (CAS).

        The WHERE clause is the lock: only a row that is still ``ready`` with no live
        claim flips, so concurrent dispatchers/workers can race and exactly one wins
        (rowcount==1). Returns the claimed Goal, or None if another worker took it.
        """
        now = self._now()
        expires = now + max(1, int(ttl_seconds))
        rc = execute_retry(
            self.db_path,
            """UPDATE goals
                  SET status='running', claim_lock=?, claim_expires=?,
                      started_at=COALESCE(started_at,?), last_heartbeat_at=?
                WHERE id=? AND status='ready' AND claim_lock IS NULL""",
            (worker, expires, now, now, goal_id),
        )
        if rc != 1:
            return None
        self._event(goal_id, "claimed", {"worker": worker})
        return self.get(goal_id)

    def heartbeat(self, goal_id: str, worker: str, *, ttl_seconds: int) -> bool:
        now = self._now()
        rc = execute_retry(
            self.db_path,
            """UPDATE goals SET last_heartbeat_at=?, claim_expires=?
                WHERE id=? AND claim_lock=? AND status='running'""",
            (now, now + max(1, int(ttl_seconds)), goal_id, worker),
        )
        return rc == 1

    def record_success(self, goal_id: str, *, session_id: Optional[str] = None,
                       result: Optional[str] = None) -> None:
        now = self._now()
        rc = execute_retry(
            self.db_path,
            """UPDATE goals
                  SET status='done', result=?, session_id=COALESCE(?,session_id),
                      consecutive_failures=0, claim_lock=NULL, claim_expires=NULL,
                      completed_at=?
                WHERE id=? AND status='running'""",
            (result, session_id, now, goal_id),
        )
        if rc != 1:
            # Owner intervened (cancel/pause) while the run was in flight — their
            # decision wins. Keep the status; archive the late result as an event.
            self._event(goal_id, "stale_completion",
                        {"result": (result or "")[:500], "session_id": session_id})
            return
        self._event(goal_id, "succeeded", {"session_id": session_id})
        self._sweep_dependents_on_completion(goal_id)
        self._release_asks(goal_id, "goal_done")

    def record_failure(self, goal_id: str, *, error: str,
                       session_id: Optional[str] = None) -> Goal:
        """Increment the failure counter; trip the circuit breaker at max_retries.

        On the breaker trip the goal goes to ``blocked`` (a human/curator must
        intervene) and a ``gave_up`` event is logged. Below the threshold it returns
        to ``ready`` for another attempt. consecutive_failures resets only on success.
        """
        # Increment the counter ATOMICALLY in SQL first (a read-modify-write in Python
        # could lose a concurrent failure and under-count the breaker), then read back
        # the authoritative value to decide stay-ready vs trip-to-blocked.
        now = self._now()
        rc = execute_retry(
            self.db_path,
            "UPDATE goals SET consecutive_failures = consecutive_failures + 1 WHERE id=? AND status='running'",
            (goal_id,),
        )
        if rc != 1:
            g = self.get(goal_id)
            if g is None:
                raise KeyError(goal_id)
            self._event(goal_id, "stale_completion", {"error": error[:500]})
            return g
        g = self.get(goal_id)
        if g is None:
            raise KeyError(goal_id)
        fails = g.consecutive_failures  # already incremented above
        # Guard these branch UPDATEs with the same 'AND status=running' CAS as the
        # increment above: between the increment and this branch, another actor
        # (owner cancel/pause) could have moved the row off 'running'. Without the
        # guard the branch would silently resurrect a cancelled/blocked-by-owner
        # goal back to 'ready' (or stomp its status to 'blocked'). If the guarded
        # UPDATE hits 0 rows, the failure counter increment above still landed
        # (harmless — max_retries accounting on a dead row is inert) but the status
        # transition is skipped and logged as a stale_completion instead.
        if fails >= g.max_retries:
            rc2 = execute_retry(
                self.db_path,
                """UPDATE goals SET status='blocked', consecutive_failures=?,
                      last_failure_error=?, session_id=COALESCE(?,session_id),
                      claim_lock=NULL, claim_expires=NULL, completed_at=?
                    WHERE id=? AND status='running'""",
                (fails, error[:2000], session_id, now, goal_id),
            )
            if rc2 == 1:
                self._event(goal_id, "gave_up", {"failures": fails, "error": error[:500]})
                self._cascade_dep_failed(goal_id)
            else:
                self._event(goal_id, "stale_completion", {"error": error[:500]})
        else:
            rc2 = execute_retry(
                self.db_path,
                """UPDATE goals SET status='ready', consecutive_failures=?,
                      last_failure_error=?, session_id=COALESCE(?,session_id),
                      claim_lock=NULL, claim_expires=NULL
                    WHERE id=? AND status='running'""",
                (fails, error[:2000], session_id, goal_id),
            )
            if rc2 == 1:
                self._event(goal_id, "failed", {"failures": fails, "error": error[:500]})
            else:
                self._event(goal_id, "stale_completion", {"error": error[:500]})
        # §5.2: keep the compact attempt ledger current (fail-open).
        self._append_attempt(goal_id, error=error, session_id=session_id)
        return self.get(goal_id)

    def block_from_ready(self, goal_id: str, *, error: str) -> bool:
        """Flip a 'ready' goal straight to 'blocked' (agent-declared BLOCKED, §3.1).

        Used when the agent itself declared the goal unrunnable (OUTCOME: BLOCKED),
        so waiting for the circuit breaker's remaining retries is pointless. The
        CAS guard (``AND status='ready'``) means an owner intervention (cancel/
        pause) that landed since record_failure always wins — a cancelled row is
        never resurrected into 'blocked'.
        """
        now = self._now()
        rc = execute_retry(
            self.db_path,
            """UPDATE goals SET status='blocked', last_failure_error=?,
                  claim_lock=NULL, claim_expires=NULL, completed_at=?
                WHERE id=? AND kind='goal' AND status='ready'""",
            (error[:2000], now, goal_id),
        )
        if rc == 1:
            self._event(goal_id, "gave_up", {"error": error[:500], "declared": True})
        return rc == 1

    def reclaim_stale(self) -> int:
        """Reclaim goals whose claim TTL expired (a crashed worker).

        H12: an expired claim means the worker died WITHOUT calling record_failure — so
        count it as a failure and route it through the same circuit breaker. Otherwise a
        goal that kills its worker (OOM/segfault/SIGKILL) is re-queued unchanged and
        crash-loops forever, never reaching 'blocked' and permanently occupying a
        concurrency slot. Below max_retries -> 'ready' (retry); at/above -> 'blocked'.
        """
        now = self._now()
        stale = "status='running' AND claim_expires IS NOT NULL AND claim_expires < ?"
        # 1) Count the crash as a failure for every expired-claim row.
        rc = execute_retry(
            self.db_path,
            f"UPDATE goals SET consecutive_failures = consecutive_failures + 1 WHERE {stale}",
            (now,),
        )
        if not rc:
            return 0
        # 2) Trip the breaker for those that reached max_retries. T2.1 Task 2: this
        #    is one of the two "breaker->blocked" sites whose dependents must
        #    cascade to dep_failed, so it goes row-by-row (was one bulk UPDATE)
        #    to know exactly which prerequisite ids just landed 'blocked'.
        trip_rows = execute_retry(
            self.db_path,
            f"SELECT id FROM goals WHERE {stale} AND consecutive_failures >= max_retries",
            (now,), fetch="all",
        ) or []
        for r in trip_rows:
            gid = r["id"]
            # This per-row CAS deliberately does NOT repeat the
            # `consecutive_failures >= max_retries` recheck from the `trip_rows`
            # SELECT above — equivalent, not a gap: nothing decrements the
            # counter while status stays 'running' (only step 1's increment
            # touches it), so a row that qualified there still qualifies here;
            # `{stale}` (status='running' AND claim_expires<now) is the only
            # guard that can meaningfully change between the two queries (an
            # owner cancel/pause moving it off 'running').
            rc_trip = execute_retry(
                self.db_path,
                f"""UPDATE goals SET status='blocked', claim_lock=NULL, claim_expires=NULL,
                       completed_at=?, last_failure_error='reclaimed: worker crashed (stale claim)'
                     WHERE id=? AND {stale}""",
                (now, gid, now),
            )
            if rc_trip == 1:
                self._cascade_dep_failed(gid)
        # 3) Re-queue the rest (still 'running' with an expired claim).
        execute_retry(
            self.db_path,
            f"""UPDATE goals SET status='ready', claim_lock=NULL, claim_expires=NULL
                 WHERE {stale} AND consecutive_failures < max_retries""",
            (now,),
        )
        return rc or 0

    def requeue_running_on_boot(self) -> int:
        """§5.1 cold-start sweep: re-queue ``running`` goals immediately at boot
        WITHOUT a failure increment — a process restart is not the goal's fault.

        Without this, a goal ``running`` across a deploy waited out its claim
        TTL and ``reclaim_stale`` counted the restart as a failure — two deploys
        mid-goal silently ``blocked`` it. Mirrors cron's reclaim of running rows
        (``cron/jobs.py::reclaim_stale_running``). Call ONCE at process start,
        before any ticker runs.

        ⚠️ CLAIM-OWNERSHIP GUARD (the sibling guard this used to lack — see
        ``reclaim_stale``'s ``claim_expires < now`` and ``hold_running``'s
        ``claim_lock=worker``). Every ``start_autonomy()`` runs this sweep, so a
        SECOND process booting against the same ``goals.db`` used to requeue a
        goal a FIRST, still-running process was executing: the row could be
        claimed and run a second time concurrently (a stream leg would carry its
        money grant twice), and the original run's ``record_success`` CAS
        (``WHERE status='running'``) then missed and degraded to a stale
        completion, so finished work was never recorded ``done``.

        A row is requeued only when its claim is NOT live:
          - no claim / no expiry, or an EXPIRED claim (the plain boundary), or
          - a still-unexpired claim whose owning process is PROVABLY gone
            (``claim_liveness.claim_owner_alive`` is ``False`` — the dispatcher
            stamps ``goal-dispatch-<pid>`` and ``goals.db`` is one host's file).
        The second arm is what keeps the documented purpose intact: a restarted
        process's predecessor is dead, so its goals come back immediately, with
        no failure increment. A claim that is unexpired AND whose owner is alive
        (or unidentifiable — ``None``, never assumed dead) is left alone; if that
        owner really did die in a way we cannot see, ``reclaim_stale`` still
        recovers the row once the TTL lapses.
        """
        from agents.task.goals.claim_liveness import claim_owner_alive
        now = self._now()
        rows = execute_retry(
            self.db_path,
            "SELECT id, claim_lock, claim_expires FROM goals "
            "WHERE status='running' AND kind='goal'",
            (), fetch="all",
        ) or []
        if not rows:
            return 0
        n = 0
        for r in rows:
            gid, lock, expires = r["id"], r["claim_lock"], r["claim_expires"]
            if expires is not None and float(expires) >= now:
                if claim_owner_alive(lock) is not False:
                    continue  # possibly-live owner — leave it for reclaim_stale
                reason = "owner process gone"
            else:
                reason = "claim expired or absent"
            # CAS on the EXACT claim identity we just judged: a heartbeat or a
            # fresh claim landing between the read and the write means the row is
            # no longer the one we cleared, and the update must not apply.
            rc = execute_retry(
                self.db_path,
                """UPDATE goals SET status='ready', claim_lock=NULL, claim_expires=NULL
                    WHERE id=? AND status='running'
                      AND claim_lock IS ? AND claim_expires IS ?""",
                (gid, lock, expires),
            )
            if rc == 1:
                n += 1
                self._event(gid, "requeued_on_boot", {"reason": reason})
        return n

    def hold_running(self, *, worker: str, reason: str) -> List[str]:
        """031 owner pause: return THIS worker's running goals to ``ready`` with the
        claim cleared and NO failure increment — a pause is not the goal's fault.
        Scoped to ``claim_lock=worker`` so a second process's runs are untouched
        (it holds its own on its next tick)."""
        rows = execute_retry(
            self.db_path,
            "SELECT id FROM goals WHERE status='running' AND kind='goal' AND claim_lock=?",
            (worker,), fetch="all",
        ) or []
        held: List[str] = []
        for r in rows:
            rc = execute_retry(
                self.db_path,
                """UPDATE goals SET status='ready', claim_lock=NULL, claim_expires=NULL
                    WHERE id=? AND status='running' AND claim_lock=?""",
                (r["id"], worker),
            )
            if rc == 1:
                held.append(r["id"])
                self._event(r["id"], "held_by_pause", {"reason": str(reason)[:200]})
        return held

    def unblock(self, goal_id: str, *, user_id: str, rationale: str = "") -> bool:
        """§5.3: requeue a ``blocked`` goal with a rationale (symmetric to
        ``fulfill_ask``). Tenant-scoped; resets the breaker so the retry budget
        is fresh. Returns False for a non-blocked row or a wrong tenant.

        T2.1: this is an explicit OWNER OVERRIDE — it re-enters ``ready``
        unconditionally, including a ``blocked`` row whose ``payload.block_kind``
        is ``dep_failed`` (a dependent whose prerequisite was cancelled or gave
        up) and regardless of whether its ``depends_on`` edges are actually
        satisfied. It does not re-check ``deps_satisfied`` — the owner's decision
        wins, same as every other owner-intervention CAS in this module.

        T2.1 final-review Fix 1: an owner ``unblock`` also clears
        ``payload.block_kind`` — "owner reset = fresh classification episode".
        Without this, ``stamp_block_kind``'s ``only_if_absent`` guard means the
        NEXT time this goal blocks it keeps the OLD kind (e.g. a stale
        ``needs_input`` surviving the unblock would refuse a genuine
        ``provider_outage`` stamp on the next trip, blocking self-heal; the
        inverse — a stale ``provider_outage`` surviving onto a hopeless goal —
        would let it silently burn requeues it should never have gotten).
        ``payload.provider_requeues``/``provider_retry_exhausted`` are
        PRESERVED across the reset — the requeue-cap ledger must survive a
        block episode, only the discriminator itself is cleared.
        """
        g = self.get(goal_id)
        if g is None:
            return False
        payload = dict(g.payload or {})
        payload.pop("block_kind", None)
        rc = execute_retry(
            self.db_path,
            """UPDATE goals SET status='ready', consecutive_failures=0,
                  claim_lock=NULL, claim_expires=NULL, completed_at=NULL, payload=?
                WHERE id=? AND kind='goal' AND status='blocked' AND user_id=?""",
            (json.dumps(payload), goal_id, user_id),
        )
        if rc == 1:
            self._event(goal_id, "unblocked", {"rationale": str(rationale)[:500]})
        return rc == 1

    def stamp_block_kind(self, goal_id: str, block_kind: str, *,
                         only_if_absent: bool = False) -> bool:
        """T2.1 Task 3: CAS-safe ``payload.block_kind`` stamp on a currently-
        ``blocked`` goal row — the payload discriminator ``age_out_blocked``
        reads to decide whether/how a blocked goal can self-heal.

        Mirrors ``decide_ask``'s read-merge-write idiom, guarded by the SAME
        CAS every mutation in this module uses: the write's
        ``WHERE status='blocked'`` means a row the owner already moved off
        ``blocked`` (an ``unblock``/``decide_ask`` racing this call) simply
        no-ops rather than resurrecting or overwriting it.

        ``only_if_absent=True`` (the ``needs_input`` producer) refuses to
        stamp over an EXISTING ``block_kind`` — ``dep_failed``/
        ``provider_outage`` are more specific classifications from producers
        that ran first and take precedence; a generic escalation must never
        clobber them. Returns False for a non-blocked row, an already-present
        kind under ``only_if_absent``, or a no-op re-stamp of the same kind
        (no duplicate event either way).
        """
        g = self.get(goal_id)
        if g is None or g.status != STATUS_BLOCKED:
            return False
        payload = dict(g.payload or {})
        existing = payload.get("block_kind")
        if existing == block_kind:
            return False
        if only_if_absent and existing:
            return False
        payload["block_kind"] = block_kind
        # Idempotence predicate (2026-07-23 validation): this UPDATE changes no
        # status, so WHERE status='blocked' alone let two concurrent sweeps both
        # get rc=1 and double-emit the event (the maintenance sweeps run before
        # the TickLock). Gating on the STORED kind makes the loser rc=0 — and
        # rejects its stale-payload write wholesale.
        rc = execute_retry(
            self.db_path,
            """UPDATE goals SET payload=? WHERE id=? AND status='blocked'
                 AND json_extract(payload,'$.block_kind') IS NOT ?""",
            (json.dumps(payload), goal_id, block_kind),
        )
        if rc == 1:
            self._event(goal_id, "block_kind_set", {"block_kind": block_kind})
        return rc == 1

    def merge_payload(self, goal_id: str, updates: Dict[str, Any]) -> bool:
        """Merge ``updates`` into a row's JSON payload, preserving every other key.

        ``stamp_block_kind`` does this for exactly one key on exactly one status.
        The stream seeder needs the general form — to adopt a legacy objective by
        stamping ``stream_id`` onto it without discarding its ``success_criteria``.
        Read-modify-write under a single ``execute_retry``. Callers live in
        ``agents/task/goals/streams.py``, which runs on the hourly stream-seeding
        tick rather than only in an interactive operator run. The write is
        idempotent — a concurrent caller racing this method re-applies the same
        merge rather than losing an update, so no additional locking is needed.
        """
        row = self.get(goal_id)
        if row is None:
            return False
        payload = dict(row.payload or {})
        payload.update(updates or {})
        execute_retry(self.db_path, "UPDATE goals SET payload=? WHERE id=?",
                      (json.dumps(payload), goal_id))
        return True

    # T2.1 Task-3 review (finding #2): a bare CAS/breaker guard does NOT bound a
    # non-sentinel provider failure — every requeue resets consecutive_failures,
    # so an ordinary (never-healing) provider death would otherwise retry every
    # ``GOAL_BLOCKED_PROVIDER_RETRY_MIN`` window forever. Hard cap, no new env
    # (a real operator knob belongs on breaker/retry tuning, not a silent
    # infinite-retry escape hatch): past this many requeues the row stops
    # self-healing and falls to the SAME legacy terminal max-age rail every
    # other blocked kind uses (finding #3, below).
    _PROVIDER_OUTAGE_MAX_REQUEUES = 16

    def age_out_blocked(self, *, max_age_days: int = 14,
                        provider_retry_min: Optional[int] = None) -> int:
        """§5.3 + T2.1 Task 3 (kind-aware aging, review-adjudicated 2026-07-23):
        sweep every currently-``blocked`` goal and act per ``payload.block_kind``:

        - ``provider_outage`` (not yet requeue-exhausted): a transient LLM/
          provider death heals on its own — requeued (``blocked`` → ``ready``
          CAS + a ``provider_outage_retried`` event, ``payload.provider_requeues``
          incremented) on a MUCH SHORTER window, ``GOAL_BLOCKED_PROVIDER_RETRY_MIN``
          MINUTES (default 30) rather than ``max_age_days``. Past
          :data:`_PROVIDER_OUTAGE_MAX_REQUEUES` requeues (review finding #2 — an
          ordinary provider death must not retry forever), the row is stamped
          ``payload.provider_retry_exhausted=true`` (once — a
          ``provider_retry_exhausted`` event fires exactly once) and falls
          through to the terminal rail below instead.
        - ``needs_input`` / ``dep_failed`` / absent / unknown / requeue-exhausted
          ``provider_outage``: NEVER auto-requeued (still needs an explicit
          owner decision — ``unblock``/``decide_ask`` — or, for ``dep_failed``,
          the DAG completion sweep) but IS still subject to the legacy terminal
          rail (review finding #3 — exempting a kind from auto-requeue must not
          ALSO exempt it from ever aging out, or the blocked population grows
          unbounded): a row older than ``max_age_days`` (measured from when it
          blocked, else creation) ages out VISIBLY to ``cancelled`` (logged
          ``aged_out``) and cascades to ``dep_failed`` dependents exactly like
          ``cancel()``/the two breaker-trip sites
          (``record_failure``/``reclaim_stale``).

        ``max_age_days<=0`` disables the WHOLE sweep (today's contract — the
        dispatcher's own call site already gates the call behind
        ``max_age_days>0``, so this only matters for a direct caller/test).
        """
        if max_age_days <= 0:
            return 0
        if provider_retry_min is None:
            try:
                from agents.task.constants import AutonomyConfig
                provider_retry_min = AutonomyConfig.goal_blocked_provider_retry_min()
            except Exception:
                provider_retry_min = 30
        now = self._now()
        cutoff_legacy = now - max_age_days * 86400
        cutoff_provider = now - max(0, int(provider_retry_min)) * 60

        # T2.1 Task-3 review (finding #3): pre-filter in SQL rather than pulling
        # EVERY blocked row into Python on every tick. A provider_outage row is
        # only ever a candidate once past its (short) provider window; every
        # other kind (incl. a requeue-exhausted provider_outage, which reads as
        # legacy from here on) only once past the (long) legacy window — so a
        # freshly-blocked row of ANY kind never reaches the Python loop at all.
        # Mirrors the established ``json_extract(metadata,'$.tenant_id')``
        # precedent (x402 invoicing) for indexing into the payload JSON blob.
        rows = execute_retry(
            self.db_path,
            """SELECT * FROM goals
                WHERE status='blocked' AND kind='goal'
                  AND (
                        (COALESCE(json_extract(payload, '$.block_kind'), '') = 'provider_outage'
                             AND COALESCE(completed_at, created_at) < ?)
                     OR (COALESCE(json_extract(payload, '$.block_kind'), '') != 'provider_outage'
                             AND COALESCE(completed_at, created_at) < ?)
                  )""",
            (cutoff_provider, cutoff_legacy), fetch="all",
        ) or []
        n = 0
        for r in rows:
            g = Goal.from_row(r)
            blocked_at = g.completed_at if g.completed_at is not None else g.created_at
            payload = dict(g.payload or {})
            block_kind = payload.get("block_kind")

            if block_kind == "provider_outage" and not payload.get("provider_retry_exhausted"):
                if blocked_at >= cutoff_provider:
                    continue  # still inside the short self-heal window
                requeues = int(payload.get("provider_requeues") or 0)
                if requeues < self._PROVIDER_OUTAGE_MAX_REQUEUES:
                    payload["provider_requeues"] = requeues + 1
                    rc = execute_retry(
                        self.db_path,
                        """UPDATE goals SET status='ready', consecutive_failures=0,
                              claim_lock=NULL, claim_expires=NULL, completed_at=NULL,
                              payload=?
                            WHERE id=? AND status='blocked'""",
                        (json.dumps(payload), g.id),
                    )
                    if rc == 1:
                        n += 1
                        self._event(g.id, "provider_outage_retried",
                                    {"provider_retry_min": provider_retry_min,
                                     "requeues": requeues + 1})
                    continue
                # Cap reached: stop self-healing, stamp ONCE, then fall through
                # below to the same terminal rail every other kind uses. The
                # stored-payload predicate makes "ONCE" hold under concurrent
                # sweeps too (2026-07-23 validation: the sweeps run before the
                # TickLock, and a payload-only UPDATE gave both writers rc=1 —
                # duplicate provider_retry_exhausted events).
                payload["provider_retry_exhausted"] = True
                rc = execute_retry(
                    self.db_path,
                    """UPDATE goals SET payload=? WHERE id=? AND status='blocked'
                         AND json_extract(payload,'$.provider_retry_exhausted') IS NOT 1""",
                    (json.dumps(payload), g.id),
                )
                if rc == 1:
                    self._event(g.id, "provider_retry_exhausted", {"requeues": requeues})

            # needs_input / dep_failed / absent / unknown / requeue-exhausted
            # provider_outage: never auto-requeued, but ALL still subject to
            # the legacy terminal max-age cutoff (review finding #3).
            if blocked_at >= cutoff_legacy:
                continue
            rc = execute_retry(
                self.db_path,
                """UPDATE goals SET status='cancelled', completed_at=?
                    WHERE id=? AND status='blocked'""",
                (now, g.id),
            )
            if rc == 1:
                n += 1
                self._event(g.id, "aged_out", {"max_age_days": max_age_days})
                self._cascade_dep_failed(g.id)
        return n

    def reconcile_waiting(self, *, limit: int = 50) -> int:
        """T2.1 final-review Fix 3: janitor for stranded ``waiting`` rows.

        Every other terminal-adjacent state has a sweep (``reclaim_stale`` for
        a crashed ``running`` row, ``age_out_blocked`` for a stale ``blocked``
        row) — ``waiting`` had none. A row can strand there in two narrow
        windows the completion sweep can't see: (a) ``create`` writes the goal
        row THEN the edges — a prerequisite that completes in that gap is
        missed (the sweep only fires from the PREREQUISITE's own
        ``record_success``, which already ran); (b) a process crash between
        ``record_success``'s CAS-to-``done`` and its dependents sweep. A
        stranded waiting row is also invisible to the planner's STALLED guard
        (``_is_live_waiting_goal`` reads it as "in flight" forever).

        Cross-tenant sweep (mirrors ``reclaim_stale``/``age_out_blocked`` — an
        internal lifecycle op, not an owner-scoped query) — cheap: one SELECT
        of ``waiting`` rows, then a FRESH ``deps_satisfied`` re-check per row
        (never a cached snapshot). Two outcomes per row:

        - every prerequisite is ``done`` -> CAS ``waiting`` -> ``ready`` +
          ``deps_satisfied`` event (``reason: "reconciled"``, distinguishing it
          from the normal completion-sweep event of the same kind).
        - any prerequisite is terminally dead (cancelled, or blocked with the
          breaker actually exhausted — mirrors ``create``'s
          ``terminal_bad_ids`` check) and can therefore never reach ``done``
          -> the row is stranded behind dead work, not live work, so it is
          cascaded to ``blocked``/``dep_failed`` (the same shape
          ``_cascade_dep_failed`` produces) rather than left waiting forever.

        Returns the number of rows transitioned (either direction).
        """
        rows = execute_retry(
            self.db_path,
            "SELECT id FROM goals WHERE status='waiting' AND kind='goal' "
            "ORDER BY created_at LIMIT ?",
            (int(limit),), fetch="all",
        ) or []
        n = 0
        for r in rows:
            gid = r["id"]
            if self.deps_satisfied(gid):
                rc = execute_retry(
                    self.db_path,
                    "UPDATE goals SET status='ready' WHERE id=? AND kind='goal' AND status='waiting'",
                    (gid,),
                )
                if rc == 1:
                    n += 1
                    self._event(gid, "deps_satisfied", {"reason": "reconciled"})
                continue
            dead = False
            for dep_id in self.dependencies(gid):
                dep = self.get(dep_id)
                if dep is None:
                    continue
                if dep.status == STATUS_CANCELLED or (
                        dep.status == STATUS_BLOCKED
                        and dep.consecutive_failures >= dep.max_retries):
                    dead = True
                    break
            if not dead:
                continue  # still genuinely waiting on live work
            g = self.get(gid)
            if g is None or g.status != STATUS_WAITING:
                continue
            payload = dict(g.payload or {})
            payload["block_kind"] = "dep_failed"
            rc = execute_retry(
                self.db_path,
                """UPDATE goals SET status='blocked', payload=?, completed_at=?
                    WHERE id=? AND kind='goal' AND status='waiting'""",
                (json.dumps(payload), self._now(), gid),
            )
            if rc == 1:
                n += 1
                self._event(gid, "dep_failed", {"reason": "reconciled"})
        return n

    _MAX_ATTEMPTS_KEPT = 5

    def _append_attempt(self, goal_id: str, *, error: str,
                        session_id: Optional[str]) -> None:
        """§5.2 attempt ledger: keep a compact per-attempt tail in the payload so
        retries stop being amnesiac (the retry prompt + goal_show read it).
        Fail-open — ledger bookkeeping never breaks failure recording."""
        try:
            g = self.get(goal_id)
            if g is None:
                return
            merged = dict(g.payload or {})
            attempts = list(merged.get("attempts") or [])
            attempts.append({
                "ts": self._now(),
                "error": str(error)[:500],
                "session_id": session_id,
            })
            merged["attempts"] = attempts[-self._MAX_ATTEMPTS_KEPT:]
            execute_retry(self.db_path, "UPDATE goals SET payload=? WHERE id=?",
                          (json.dumps(merged), goal_id))
        except Exception:
            logger.debug("attempt-ledger append failed for %s", goal_id, exc_info=True)

    def count_started_since(self, seconds: float) -> int:
        """Goal runs STARTED in the trailing window (quota accounting)."""
        since = self._now() - max(0, seconds)
        row = execute_retry(
            self.db_path,
            "SELECT COUNT(*) AS n FROM goals WHERE kind='goal' AND started_at IS NOT NULL AND started_at > ?",
            (since,), fetch="one",
        )
        if not row:
            return 0
        try:
            return int(row["n"])
        except (KeyError, TypeError, IndexError):
            return int(row[0])

    def set_outcome(self, goal_id: str, outcome: str) -> bool:
        """Attach the extracted OUTCOME note to a goal (any status, incl. done).

        A direct payload write (NOT ``update_fields``, which refuses terminal rows) —
        this runs right after a goal completes, so it must work on a ``done`` row.
        """
        g = self.get(goal_id)
        if g is None:
            return False
        merged = dict(g.payload or {})
        merged["outcome"] = outcome[:1000]
        rc = execute_retry(self.db_path, "UPDATE goals SET payload=? WHERE id=?",
                           (json.dumps(merged), goal_id))
        return rc == 1

    def count_running(self) -> int:
        """Count goals currently claimed+running (excluding expired claims).

        Authoritative CROSS-PROCESS in-flight count for enforcing
        GOAL_MAX_CONCURRENT under workers>1 (the dispatcher's per-process
        ``self._inflight`` set cannot see other workers' running goals, so the cap
        would otherwise be enforced per-worker => cap x num_workers total). Call
        under the tick lock, after reclaim_stale, so expired claims are already
        re-queued and this count is authoritative for the tick.
        """
        now = self._now()
        row = execute_retry(
            self.db_path,
            """SELECT COUNT(*) AS n FROM goals
                WHERE status='running'
                  AND (claim_expires IS NULL OR claim_expires > ?)""",
            (now,),
            fetch="one",
        )
        if not row:
            return 0
        try:
            return int(row["n"])
        except (KeyError, TypeError, IndexError):
            return int(row[0])

    def count_running_by_objective(self) -> Dict[str, int]:
        """Live in-flight goal count PER objective, keyed by ``parent_id``.

        ``count_running`` answers "how many goals are running" — enough for the
        global GOAL_MAX_CONCURRENT ceiling, but not enough to stop ONE objective
        taking every slot while its neighbours wait. Goals with no parent share the
        single ``""`` bucket: they are one undifferentiated stream, not many.
        Expired claims are excluded on the same terms as ``count_running``, so call
        this after ``reclaim_stale`` and under the tick lock for an authoritative
        number.
        """
        now = self._now()
        rows = execute_retry(
            self.db_path,
            """SELECT COALESCE(parent_id,'') AS k, COUNT(*) AS n FROM goals
                WHERE kind='goal' AND status='running'
                  AND (claim_expires IS NULL OR claim_expires > ?)
                GROUP BY k""",
            (now,),
            fetch="all",
        ) or []
        out: Dict[str, int] = {}
        for r in rows:
            try:
                key, n = r["k"], r["n"]
            except (KeyError, TypeError, IndexError):
                key, n = r[0], r[1]
            out[key or ""] = int(n)
        return out

    def cancel(self, goal_id: str, *, user_id: Optional[str] = None) -> bool:
        # kind guard: objectives are dropped (set_objective_status), never 'cancelled' —
        # without it goal_cancel on an objective id writes a status outside its enum.
        sql = ("UPDATE goals SET status='cancelled', claim_lock=NULL "
               "WHERE id=? AND kind='goal' AND status NOT IN ('done','cancelled')")
        params: tuple = (goal_id,)
        if user_id is not None:
            sql += " AND user_id=?"
            params = (goal_id, user_id)
        rc = execute_retry(self.db_path, sql, params)
        if rc == 1:
            self._event(goal_id, "cancelled", {})
            self._cascade_dep_failed(goal_id)
            self._release_asks(goal_id, "goal_cancelled")
        return rc == 1

    def update_status(self, goal_id: str, new_status: str, *, reset_failures: bool = False,
                      user_id: Optional[str] = None) -> bool:
        """Update goal status, optionally resetting failure counters.

        T2.1 Task 5 hardening: ``user_id`` is an OPTIONAL tenant guard, default
        ``None`` — every pre-existing internal caller (breaker/aging/sweep code
        in this module, which already reasons about tenancy elsewhere) is
        byte-identical. When a caller passes ``user_id`` (the CLI, `polyrob
        goals ready/pause/resume/retry` — this was IDOR-shaped before: any
        caller could flip any tenant's goal status by id), the UPDATE adds
        ``AND user_id=?`` so a wrong/foreign tenant is a harmless no-op
        (``rc=0``), not a cross-tenant mutation.
        """
        sets = ["status=?", "claim_lock=NULL", "claim_expires=NULL"]
        params: List[Any] = [new_status, goal_id]
        if reset_failures:
            sets.append("consecutive_failures=0")
            sets.append("last_failure_error=NULL")
        sql = f"UPDATE goals SET {', '.join(sets)} WHERE id=?"
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        rc = execute_retry(self.db_path, sql, tuple(params))
        if rc == 1:
            self._event(goal_id, f"status_{new_status}", {})
        return rc == 1

    # --- dependency graph (DAG, T2.1 Task 1) ----------------------------------

    def _validate_dep_ids(self, dep_ids: List[str], *, user_id: str) -> List[Goal]:
        """Resolve dep ids to Goal rows, enforcing existence + same-tenant +
        ``kind='goal'``.

        Raises ValueError on the first bad id — an all-or-nothing validation
        pass (a partially-validated dep set is worse than none). ``get()`` is
        NOT tenant-scoped, so every dep is re-checked against ``user_id`` here.
        The kind check matters beyond "edges are goal->goal by design":
        ``OBJ_DONE == STATUS_DONE == "done"`` (same literal), so an objective
        (or an ask, which never reaches a real 'done' status either) could
        otherwise silently satisfy a dependency it was never meant to.
        """
        deps: List[Goal] = []
        for dep_id in dep_ids:
            dep = self.get(dep_id)
            if dep is None:
                raise ValueError(f"depends_on: unknown goal id {dep_id!r}")
            if dep.user_id != user_id:
                raise ValueError(f"depends_on: goal {dep_id!r} belongs to another tenant")
            if dep.kind != KIND_GOAL:
                raise ValueError(
                    f"depends_on: {dep_id!r} is a {dep.kind!r}, not a goal — "
                    "dependency edges must target kind='goal'")
            deps.append(dep)
        return deps

    def _would_close_cycle(self, goal_id: str, dep_id: str, *, max_depth: int = 50) -> bool:
        """True if adding the edge ``goal_id -> dep_id`` ("goal_id depends on
        dep_id") would close a cycle — i.e. ``dep_id`` already transitively
        depends on ``goal_id``.

        Plain BFS over the existing ``goal_edges`` graph, depth-capped (the
        board is small; the cap guards against a pathological/corrupt edge
        set looping forever rather than raising).
        """
        frontier = {dep_id}
        visited: set = set()
        depth = 0
        while frontier and depth < max_depth:
            if goal_id in frontier:
                return True
            depth += 1
            next_frontier: set = set()
            for node in frontier:
                if node in visited:
                    continue
                visited.add(node)
                rows = execute_retry(
                    self.db_path,
                    "SELECT depends_on_id FROM goal_edges WHERE goal_id=?",
                    (node,), fetch="all",
                ) or []
                next_frontier.update(r["depends_on_id"] for r in rows)
            frontier = next_frontier
        return goal_id in frontier

    def add_dependencies(self, goal_id: str, dep_ids: List[str], *, user_id: str) -> None:
        """Record that ``goal_id`` depends on each id in ``dep_ids`` (idempotent
        per pair — ``INSERT OR IGNORE``).

        All-or-nothing: every dep is validated to exist and belong to the SAME
        tenant, and every proposed edge is checked against the existing graph
        for a self-dependency or a cycle — ALL before any edge is written.
        Already-``done`` deps are recorded too (not skipped): ``deps_satisfied``
        is a live join against current goal status, never a cached snapshot,
        so a done dep is simply always-satisfied going forward.
        """
        ids = list(dict.fromkeys(dep_ids or []))
        if not ids:
            return
        for dep_id in ids:
            if dep_id == goal_id:
                raise ValueError(f"goal {goal_id!r} cannot depend on itself")
        self._validate_dep_ids(ids, user_id=user_id)
        for dep_id in ids:
            if self._would_close_cycle(goal_id, dep_id):
                raise ValueError(
                    f"depends_on: goal {dep_id!r} would create a dependency cycle "
                    f"with {goal_id!r}")
        now = self._now()
        inserted: List[str] = []
        for dep_id in ids:
            rc = execute_retry(
                self.db_path,
                """INSERT OR IGNORE INTO goal_edges (goal_id, depends_on_id, user_id, created_at)
                    VALUES (?, ?, ?, ?)""",
                (goal_id, dep_id, user_id, now),
            )
            if rc == 1:
                inserted.append(dep_id)
        # TOCTOU repair (2026-07-23 validation): the pre-insert cycle check is
        # check-then-write, so two concurrent add_dependencies (A→B and B→A)
        # can both pass it — and a formed cycle strands both rows forever
        # (ready() excludes them, reconcile_waiting reads a waiting dep as
        # live, no janitor detects cycles). Re-verify AFTER our edges are
        # visible: the racer whose insert commits last sees the full graph, so
        # at least one racer detects the cycle, removes ITS OWN just-inserted
        # edges (never pre-existing ones), and raises — a cycle can never
        # persist past the losing caller's return.
        for dep_id in ids:
            if self._would_close_cycle(goal_id, dep_id):
                for d in inserted:
                    execute_retry(
                        self.db_path,
                        "DELETE FROM goal_edges WHERE goal_id=? AND depends_on_id=?",
                        (goal_id, d),
                    )
                raise ValueError(
                    f"depends_on: goal {dep_id!r} would create a dependency cycle "
                    f"with {goal_id!r} (detected post-insert)")
        # Edge-only writes are otherwise invisible to the wake-gate fingerprint
        # (MAX(goal_events.id)) — every mutation path must emit an event.
        self._event(goal_id, "deps_added", {"deps": ids})

    def dependencies(self, goal_id: str) -> List[str]:
        """Raw prerequisite ids for ``goal_id`` (edge list, no status join)."""
        rows = execute_retry(
            self.db_path,
            "SELECT depends_on_id FROM goal_edges WHERE goal_id=? ORDER BY created_at",
            (goal_id,), fetch="all",
        ) or []
        return [r["depends_on_id"] for r in rows]

    def dependents(self, goal_id: str) -> List[str]:
        """Ids of goals that list ``goal_id`` in their ``depends_on`` (reverse
        edges — i.e. what ``goal_id`` blocks). Public read-only wrapper over
        :meth:`_dependents_of` for callers outside this module (e.g. `goal_show`)."""
        return self._dependents_of(goal_id)

    def deps_satisfied(self, goal_id: str) -> bool:
        """True iff every prerequisite edge for ``goal_id`` currently points to
        a ``done`` goal. Always a FRESH read against ``goals.status`` — never
        cached — so callers (e.g. the completion sweep) can re-verify at write
        time rather than trust a stale snapshot."""
        row = execute_retry(
            self.db_path,
            """SELECT COUNT(*) AS n FROM goal_edges e
                JOIN goals g ON g.id = e.depends_on_id
                WHERE e.goal_id=? AND g.status != 'done'""",
            (goal_id,), fetch="one",
        )
        if not row:
            return True
        try:
            return int(row["n"]) == 0
        except (KeyError, TypeError, IndexError):
            return int(row[0]) == 0

    def _dependents_of(self, prerequisite_id: str) -> List[str]:
        """Ids of goals that list ``prerequisite_id`` in their ``depends_on``."""
        rows = execute_retry(
            self.db_path,
            "SELECT DISTINCT goal_id FROM goal_edges WHERE depends_on_id=?",
            (prerequisite_id,), fetch="all",
        ) or []
        return [r["goal_id"] for r in rows]

    def _sweep_dependents_on_completion(self, completed_goal_id: str) -> None:
        """T2.1 Task 2 completion sweep: called ONLY after ``completed_goal_id``'s
        own CAS to 'done' has already won (never on a ``stale_completion``).

        For each dependent edge, ``deps_satisfied`` is re-verified FRESH (never
        trusting a cached snapshot — a sibling prerequisite may complete between
        the check and the write) and the flip is itself a CAS
        (``WHERE status='waiting'``). Two concurrent completions of sibling
        prerequisites (e.g. via two ``GoalBoard`` instances on one db path) can
        both observe ``deps_satisfied() == True`` and both attempt the flip —
        exactly one wins (rc=1, emits the event); the other's rc=0 is harmless
        (the dependent is already ``ready``).
        """
        for dep_id in self._dependents_of(completed_goal_id):
            if not self.deps_satisfied(dep_id):
                continue
            rc = execute_retry(
                self.db_path,
                "UPDATE goals SET status='ready' WHERE id=? AND kind='goal' AND status='waiting'",
                (dep_id,),
            )
            if rc == 1:
                self._event(dep_id, "deps_satisfied", {"completed": completed_goal_id})
                continue
            # T2.1 final-review Fix 2: a dependent that was EARLIER cascaded to
            # blocked/dep_failed (a sibling prerequisite that broke the
            # breaker, or was cancelled) is still DAG-revivable once every
            # prerequisite lands 'done' — e.g. the breaker-blocked sibling is
            # owner-unblocked and later completes for real. The plan contract
            # says dep_failed is "owner/DAG-mediated"; without this the DAG
            # half was missing and a revivable dep_failed row needed a SECOND
            # manual owner unblock even after its blocker resolved itself.
            # Read-merge-write to clear block_kind, then re-verify the same
            # CAS condition at write time (dep_failed is exclusively
            # DAG-produced, so clearing it here can never clobber a more
            # specific human-facing kind).
            dep = self.get(dep_id)
            if dep is None or dep.status != STATUS_BLOCKED:
                continue
            if (dep.payload or {}).get("block_kind") != "dep_failed":
                continue
            payload = dict(dep.payload or {})
            payload.pop("block_kind", None)
            rc2 = execute_retry(
                self.db_path,
                """UPDATE goals SET status='ready', payload=?
                    WHERE id=? AND kind='goal' AND status='blocked'
                      AND COALESCE(json_extract(payload, '$.block_kind'), '') = 'dep_failed'""",
                (json.dumps(payload), dep_id),
            )
            if rc2 == 1:
                self._event(dep_id, "deps_satisfied", {"completed": completed_goal_id})

    def _cascade_dep_failed(self, prerequisite_id: str) -> None:
        """T2.1 Task 2 inverse cascade: called ONLY after ``prerequisite_id``'s
        own CAS to a terminal-bad status (cancelled, or blocked via a breaker
        trip) has already won.

        A ``waiting`` dependent can never satisfy a dead prerequisite (it will
        never reach 'done'), so it is flipped ``waiting -> blocked`` (CAS) with
        ``payload.block_kind='dep_failed'`` — visible and owner-unblockable
        (see :meth:`unblock`) rather than sitting in ``waiting`` forever.
        """
        now = self._now()
        for dep_id in self._dependents_of(prerequisite_id):
            dep = self.get(dep_id)
            if dep is None or dep.status != STATUS_WAITING:
                continue
            payload = dict(dep.payload or {})
            payload["block_kind"] = "dep_failed"
            rc = execute_retry(
                self.db_path,
                """UPDATE goals SET status='blocked', payload=?, completed_at=?
                    WHERE id=? AND kind='goal' AND status='waiting'""",
                (json.dumps(payload), now, dep_id),
            )
            if rc == 1:
                self._event(dep_id, "dep_failed", {"prerequisite": prerequisite_id})

    # --- queries -------------------------------------------------------------

    def get(self, goal_id: str, *, user_id: Optional[str] = None) -> Optional[Goal]:
        """One row by id. ``user_id`` is an optional tenant filter (B25, S9
        2026-08-29): a tenant-facing caller (the goal tool, the approval queue)
        passes it so a foreign id reads as "not found" instead of returning the
        row for the caller to inspect; internal callers that already hold their
        own rows keep the unscoped form."""
        if user_id is not None:
            row = execute_retry(self.db_path, "SELECT * FROM goals WHERE id=? AND user_id=?",
                                (goal_id, user_id), fetch="one")
        else:
            row = execute_retry(self.db_path, "SELECT * FROM goals WHERE id=?", (goal_id,), fetch="one")
        return Goal.from_row(row) if row else None

    def list(self, *, user_id: Optional[str] = None, status: Optional[str] = None,
             limit: int = 100) -> List[Goal]:
        sql = "SELECT * FROM goals"
        clauses, params = [], []
        if user_id is not None:
            clauses.append("user_id=?"); params.append(user_id)
        if status is not None:
            clauses.append("status=?"); params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, created_at LIMIT ?"
        params.append(int(limit))
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def list_recent(self, *, user_id: str, statuses: Optional[tuple] = None,
                    limit: int = 30) -> List[Goal]:
        """This tenant's GOAL rows, newest first, optionally filtered by status.

        :meth:`list` is ``ORDER BY priority DESC, created_at ASC LIMIT ?`` — a
        dispatcher-shaped order. Used as a *view* it is wrong: on the 409-row prod
        board (2026-08-29) the agent's ``goal_list`` window was the OLDEST 100 rows
        and held zero of the manifest's stream legs (priority 2/3 sort after every
        priority-5 row), so the agent told the owner it had no trading goals while
        ten clean cycles had run. Any "what is on my board" surface reads this.
        """
        sql = "SELECT * FROM goals WHERE kind=? AND user_id=?"
        params: List[Any] = [KIND_GOAL, user_id]
        if statuses:
            sql += " AND status IN (%s)" % ",".join("?" for _ in statuses)
            params.extend(statuses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def status_counts(self, *, user_id: str) -> Dict[str, int]:
        """``{status: n}`` over EVERY goal row of this tenant — never a window."""
        rows = execute_retry(
            self.db_path,
            "SELECT status, COUNT(*) AS n FROM goals WHERE kind=? AND user_id=? "
            "GROUP BY status",
            (KIND_GOAL, user_id), fetch="all") or []
        out: Dict[str, int] = {}
        for r in rows:
            try:
                out[str(r["status"])] = int(r["n"])
            except (KeyError, TypeError, IndexError):
                out[str(r[0])] = int(r[1])
        return out

    def has_live_goals(self, *, user_id: str) -> bool:
        """True while any goal of this tenant is in flight (triage/waiting/ready/
        running). ``blocked`` is NOT live here: it needs an owner, it is not
        work that will finish on its own."""
        row = execute_retry(
            self.db_path,
            "SELECT 1 AS x FROM goals WHERE kind=? AND user_id=? AND status IN (?,?,?,?) "
            "LIMIT 1",
            (KIND_GOAL, user_id, STATUS_TRIAGE, STATUS_WAITING, STATUS_READY,
             STATUS_RUNNING), fetch="one")
        return row is not None

    def count_created_by_session(self, session_id: str) -> int:
        """Goals *session_id* created — how many a planner run actually queued,
        read from the board rather than from the model's own summary text.
        Matches the always-stamped audit id ``payload.created_by_session_id``
        (031) OR the legacy wake-target ``payload.origin_session_id`` (stamped
        only for a genuine owner turn, so a planner's own goals never carried it
        and its outcome accounting under-counted)."""
        row = execute_retry(
            self.db_path,
            "SELECT COUNT(*) AS n FROM goals WHERE kind=? AND ("
            "json_extract(payload,'$.created_by_session_id')=? OR "
            "json_extract(payload,'$.origin_session_id')=?)",
            (KIND_GOAL, str(session_id), str(session_id)), fetch="one")
        try:
            return int(row["n"]) if row else 0
        except (KeyError, TypeError, IndexError):
            return int(row[0]) if row else 0

    def ready(self, *, limit: int = 10) -> List[Goal]:
        """Ready goals across all tenants, highest priority first (dispatcher feed)."""
        rows = execute_retry(
            self.db_path,
            """SELECT * FROM goals WHERE status='ready' AND claim_lock IS NULL AND kind='goal'
                ORDER BY priority DESC, created_at LIMIT ?""",
            (int(limit),), fetch="all",
        ) or []
        return [Goal.from_row(r) for r in rows]

    def ready_fair(self, *, limit: int, per_objective_cap: int = 0,
                   in_flight: Optional[Dict[str, int]] = None) -> List[Goal]:
        """Ready goals, round-robined across objectives instead of globally ordered.

        ``ready()`` sorts the WHOLE board by ``priority DESC, created_at``, so one
        objective's backlog takes every dispatch slot while its neighbours starve —
        tolerable at 6 standing objectives, the dominant failure at 16. This walks
        the same ordered window in PASSES, taking at most one goal per objective per
        pass. Priority therefore still decides who is served first; it no longer
        decides who is served at all.

        Throughput is preserved: with a single active objective every pass takes
        from it, so all ``limit`` slots still fill.

        ``per_objective_cap`` is an OPTIONAL additional ceiling on how many goals one
        objective may have in flight at once (``<=0`` disables it, the default).
        ``in_flight`` is the caller's ``count_running_by_objective()`` snapshot; it
        is only consulted when a cap is set. Goals with no parent share the ``""``
        bucket — they are one stream, not many.
        """
        if limit <= 0:
            return []
        counts = dict(in_flight or {})
        buckets: "OrderedDict[str, List[Goal]]" = OrderedDict()
        for g in self.ready(limit=READY_SCAN_LIMIT):
            buckets.setdefault(g.parent_id or "", []).append(g)
        picked: List[Goal] = []
        taken: Dict[str, int] = {}
        while len(picked) < limit:
            progressed = False
            for key, queue in buckets.items():
                if len(picked) >= limit:
                    break
                if not queue:
                    continue
                if per_objective_cap > 0 and \
                        counts.get(key, 0) + taken.get(key, 0) >= per_objective_cap:
                    continue
                picked.append(queue.pop(0))
                taken[key] = taken.get(key, 0) + 1
                progressed = True
            if not progressed:
                break
        return picked

    def events(self, goal_id: str) -> List[Dict[str, Any]]:
        rows = execute_retry(
            self.db_path, "SELECT * FROM goal_events WHERE goal_id=? ORDER BY id",
            (goal_id,), fetch="all",
        ) or []
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.get("payload") or "{}")
            out.append(d)
        return out

    def children_of(self, user_id: str, objective_id: str,
                    *, live_only: bool = True) -> List[Goal]:
        """Goals attached to *objective_id*. ``live_only`` drops cancelled/dropped
        rows — the budget counts live work, not history."""
        sql = ("SELECT * FROM goals WHERE kind=? AND user_id=? AND parent_id=?")
        params: List[Any] = [KIND_GOAL, user_id, objective_id]
        if live_only:
            sql += " AND status NOT IN ('cancelled','dropped')"
        rows = execute_retry(self.db_path, sql + " ORDER BY created_at",
                             tuple(params), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def stream_goals(self, user_id: str, stream_id: str) -> List[Goal]:
        """Every goal row tagged for *stream_id*, tenant-scoped, filtered in SQL.

        The stream throttles (``streams.stream_live_goals`` /
        ``streams.stream_last_seeded_at``) used to derive this from
        ``list(user_id=..., limit=1000)``. That is unsafe: ``list`` ends
        ``ORDER BY priority DESC, created_at LIMIT ?``, so the rows evicted from a
        bounded window are the LOWEST-priority ones — and a manifest stream's legs
        sit BELOW the board default priority of 5 by design. Unrelated ordinary
        rows therefore push a stream's own LIVE rows out of the window first, and
        both throttles invert from "wait" to "seed again". Filtering by tag in SQL
        cannot be evicted by anything.

        TWO tags are matched, not one. ``payload.stream`` is what
        ``streams.seed_stream`` writes; ``payload.cycle`` is what the legacy
        per-stream seeder (``scripts/seed_trading_cycle.py``, whose ``CYCLE_TAG``
        is literally ``"treasury-trading"`` — the same string as the manifest
        stream id) wrote. During the cutover BOTH seeders can be armed at once,
        and a throttle blind to the other one's tag green-lights a second,
        overlapping cycle of the same stream — for the trading stream, two
        concurrent grants of ``defi_trade``. Once the legacy seeder is deleted
        nothing writes ``payload.cycle`` and the second clause matches nothing,
        so this stays harmless rather than needing to be undone.
        """
        rows = execute_retry(
            self.db_path,
            """SELECT * FROM goals
                WHERE kind=? AND user_id=?
                  AND (json_extract(payload,'$.stream')=?
                       OR json_extract(payload,'$.cycle')=?)
                ORDER BY created_at""",
            (KIND_GOAL, user_id, stream_id, stream_id), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def objective_budget(self, objective: Goal) -> int:
        """Live-goal cap for *objective*. 0 disables. The objective's own
        ``payload.goal_budget`` wins over the deployment default, so a
        deliberately long-running objective can state its number.

        A STREAM objective — one carrying ``payload.stream_id``, written by the
        declarative manifest seeder (``agents/task/goals/streams.py``) — is
        ALWAYS uncapped. This budget is a LIFETIME tally: ``children_of(...,
        live_only=True)`` drops only ``cancelled``/``dropped``, so every ``done``
        child counts against it forever and nothing ever sweeps them. That is
        exactly right for a BOUNDED PROJECT (it is what stops the planner opening
        "Round 9" on an objective that never finishes) and exactly wrong for a
        PERMANENT stream, whose whole contract is to re-seed the same cycle for
        as long as it stands: a 3-leg cycle under a budget of 12 jams after four
        cycles and the stream dies silently — including the trading stream, whose
        legs are the only operator-granted path to a money verb. A manifest
        stream is already throttled by its own ``max_live_goals`` +
        ``cadence_hours``, which bound CONCURRENT work rather than lifetime work
        and are a strictly better rail for standing work. A manifest
        ``objective.goal_budget`` is still recorded on the row (it takes effect
        again if the stream identity is ever removed); it is simply not enforced
        while the row IS a stream.
        """
        if (objective.payload or {}).get("stream_id"):
            return 0
        own = (objective.payload or {}).get("goal_budget")
        if own is not None:
            try:
                return max(0, int(own))
            except (TypeError, ValueError):
                pass
        from core.env import int_env
        return int_env("OBJECTIVE_GOAL_BUDGET", 25)

    def _check_objective_budget(self, user_id: str, parent_id: Optional[str]) -> None:
        """Refuse a new child once an objective has spent its budget.

        An objective with no completion criterion and no cap forces the planner
        to invent the next increment forever: prod's "Explore the x402 agent
        economy" accumulated 32 children over a month and produced Rounds 1..9
        while its actual purpose (revenue) produced $0. The cap turns "keep
        going" into "come back to me" — the refusal message says so explicitly,
        because the agent's next move should be an ask, not another round.

        Fail-OPEN on any lookup error: a budget check must never be the reason a
        legitimate goal cannot be filed.
        """
        if not parent_id:
            return
        try:
            objective = self.get(parent_id)
            if objective is None or objective.kind != KIND_OBJECTIVE:
                return
            budget = self.objective_budget(objective)
            if budget <= 0:
                return
            live = len(self.children_of(user_id, parent_id))
        except Exception:
            logger.debug("objective budget check skipped for %s", parent_id, exc_info=True)
            return
        if live >= budget:
            raise ValueError(
                f"objective {parent_id} has spent its goal budget ({live}/{budget} live "
                f"goals). Do NOT open another round on it. Either finish or cancel the "
                f"open goals, or raise an ask explaining what decision you need from the "
                f"owner to complete the objective.")

    def create_objective(self, *, user_id: str, title: str, body: str = "",
                         priority: int = 5, force: bool = False,
                         payload: Optional[Dict[str, Any]] = None) -> Goal:
        """An objective is a standing, never-dispatched row goals attach to.

        ``payload`` may carry ``success_criteria`` (§7.3) so the planner measures
        against what the owner actually wants, not a self-set proxy.
        """
        return self.create(user_id=user_id, title=title, body=body, priority=priority,
                           kind=KIND_OBJECTIVE, status=OBJ_ACTIVE, force=force,
                           payload=payload)

    # --- asks (§7.2b) ---------------------------------------------------------

    def add_ask_blocked_goals(self, ask_id: str, goal_ids) -> list:
        """Merge *goal_ids* into an EXISTING ask's ``blocks_goal_ids``.

        The fuzzy-dedup branch of :meth:`create_ask` has always done this inline;
        a caller that runs its OWN exact-match dedup (``owner_queue`` keys on a
        request hash and reuses the open ask) had no way to, so a re-asked
        approval kept whatever goal list it was first created with — usually
        none. The owner then approved an ask that re-armed nothing (039).

        Returns the merged list. Never raises: failing to widen the list must not
        stop an approval being requested.
        """
        wanted = [g for g in (goal_ids or []) if g]
        if not wanted:
            return []
        try:
            row = self.get(ask_id)
            if row is None:
                return []
            payload = dict(row.payload or {})
            merged = sorted(set(payload.get("blocks_goal_ids") or []) | set(wanted))
            if merged == sorted(payload.get("blocks_goal_ids") or []):
                return merged
            payload["blocks_goal_ids"] = merged
            execute_retry(self.db_path, "UPDATE goals SET payload=? WHERE id=?",
                          (json.dumps(payload), ask_id))
            return merged
        except Exception:
            logger.warning("could not widen blocks_goal_ids on ask %s", ask_id,
                           exc_info=True)
            return []

    def create_ask(self, *, user_id: str, what: str, why: str = "",
                   blocks_goal_ids: Optional[List[str]] = None,
                   objective_id: Optional[str] = None,
                   extra_payload: Optional[Dict[str, Any]] = None,
                   force: bool = False) -> Goal:
        """A durable owner-facing need ("I require X from you to proceed").

        Never dispatched (``ready()`` filters ``kind='goal'``). Dedups ONLY against
        this tenant's OPEN asks — a matching one is refreshed (its dependent-goal
        set unioned) rather than respawned, so a recurring blocker stays ONE ask.

        ``extra_payload`` (Task 9 / G-2) merges additional keys into the created
        payload atomically at creation (e.g. a ``tool_approval`` ask's discriminator
        + stable request hash) — no separate post-create UPDATE, no read/write race.
        ``force=True`` skips the fuzzy title-similarity dedup above: a caller that
        already does its own EXACT-match dedup (e.g. by request hash) needs this,
        since two distinct requests can otherwise share a near-identical generic
        title (e.g. "Approve x402_request?") and get fuzzy-merged into one ask.
        """
        from agents.task.constants import AutonomyConfig
        threshold = AutonomyConfig.goal_dedup_threshold()
        blocks = list(blocks_goal_ids or [])
        if not force and threshold > 0:
            for a in self.asks(user_id=user_id, status=ASK_OPEN):
                if title_similarity(what, a.title) >= threshold:
                    merged = sorted(set((a.payload or {}).get("blocks_goal_ids", [])) | set(blocks))
                    payload = dict(a.payload or {})
                    payload["blocks_goal_ids"] = merged
                    execute_retry(self.db_path, "UPDATE goals SET payload=? WHERE id=?",
                                  (json.dumps(payload), a.id))
                    self._event(a.id, "ask_refreshed", {"what": what[:200]})
                    return self.get(a.id)
        payload: Dict[str, Any] = {"blocks_goal_ids": blocks}
        if extra_payload:
            payload.update(extra_payload)
        return self.create(user_id=user_id, title=what, body=why, kind=KIND_ASK,
                           status=ASK_OPEN, parent_id=objective_id, force=True,
                           payload=payload)

    #: ``payload.kind`` discriminator for the budget-exhaustion ask below, so the
    #: "does one already exist" check is an EXACT match on a stable key rather
    #: than fuzzy title similarity against unrelated asks.
    ASK_KIND_OBJECTIVE_SPENT = "objective_budget_spent"

    def escalate_spent_objectives(self, *, user_id: str) -> List[str]:
        """Raise ONE open ask per active objective that has spent its goal budget.

        ``_check_objective_budget`` refuses the child and tells the AGENT to raise
        an ask, and the planner prompt repeats it — but both are prompt-shaped, so
        whether the owner ever hears about it depends on the model complying. Live
        evidence (prod, 2026-08-28): two of four active objectives sat at 25/25 for
        days, the planner could open no work on either, and no ask was ever raised.
        A budget-exhausted objective is a STALLED objective; the owner learning
        about it must not be model-dependent.

        Idempotent: an existing OPEN ask carrying this payload kind for the same
        objective is left alone (no refresh churn). Returns the ask ids created.
        Fail-open per objective — this runs on the dispatch tick and must never be
        the reason a tick fails.
        """
        created: List[str] = []
        try:
            objectives = [o for o in self.list(user_id=user_id, status=OBJ_ACTIVE,
                                               limit=200)
                          if o.kind == KIND_OBJECTIVE]
            open_asks = self.asks(user_id=user_id, status=ASK_OPEN)
        except Exception:
            logger.debug("spent-objective escalation skipped (board read failed)",
                         exc_info=True)
            return created
        spent_asks = {(a.payload or {}).get("objective_id"): a
                      for a in open_asks
                      if (a.payload or {}).get("kind") == self.ASK_KIND_OBJECTIVE_SPENT}
        for o in objectives:
            try:
                budget = self.objective_budget(o)
                spent = False
                live_children: List[Goal] = []
                if budget > 0:  # a stream is uncapped by design
                    live_children = self.children_of(user_id, o.id)
                    spent = len(live_children) >= budget
                existing = spent_asks.get(o.id)
                if existing is not None:
                    if not spent:
                        # The NEED went away — the objective became a stream, its
                        # budget was raised, or children were cancelled. An ask
                        # that outlives its condition is a permanent, misleading
                        # "owner decision pending" (prod 2026-08-29: the planner
                        # cited two such asks as REAL BLOCKERs every hour).
                        self._obsolete_ask(existing.id, user_id=user_id,
                                           reason="objective_no_longer_spent",
                                           payload=dict(existing.payload or {}))
                    continue
                if not spent:
                    continue
                ask = self.create_ask(
                    user_id=user_id,
                    what=f"Objective at its goal budget: {o.title}",
                    why=(f"'{o.title}' has {len(live_children)}/{budget} live goals — its "
                         f"lifetime budget. The planner can open NO further work on it, so "
                         f"it is stalled, not finished. Decide one: drop it "
                         f"(`/goal objective drop {o.id}`) if it is done; raise its "
                         f"`goal_budget` if it should continue; or convert it to a stream "
                         f"for standing recurring work."),
                    blocks_goal_ids=[g.id for g in live_children[:20]],
                    objective_id=o.id,
                    extra_payload={"kind": self.ASK_KIND_OBJECTIVE_SPENT,
                                   "objective_id": o.id,
                                   "live": len(live_children), "budget": budget},
                    force=True)  # exact-match dedup above; skip fuzzy title merging
                if ask is not None:
                    created.append(ask.id)
                    self._event(o.id, "objective_budget_spent",
                                {"live": len(live_children), "budget": budget,
                                 "ask_id": ask.id})
            except Exception:
                logger.debug("spent-objective escalation failed for %s", o.id,
                             exc_info=True)
        return created

    def asks(self, *, user_id: str, status: Optional[str] = None) -> List[Goal]:
        sql = "SELECT * FROM goals WHERE kind=? AND user_id=?"
        params: List[Any] = [KIND_ASK, user_id]
        if status is not None:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY created_at"
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def decide_ask(self, ask_id: str, *, user_id: str, approved: bool) -> tuple:
        """Record an owner decision (approve or reject) on an OPEN ask.

        Generalizes :meth:`fulfill_ask` (Task 9 / G-2 — a ``tool_approval`` ask
        needs a real reject outcome, not just fulfilled/still-open). Approving
        flips BLOCKED dependent goals back to ready (the original unblock hop);
        rejecting never touches dependent goals. The decision is also stamped
        into the ask's own ``payload.decision`` (``"approved"``/``"rejected"``)
        so a poller (e.g. ``OwnerQueueApprover``) can read the outcome straight
        off the row without a second status vocabulary. Tenant-scoped CAS —
        only an OPEN ask for THIS ``user_id`` transitions. Returns
        ``(ok, unblocked_count)``.
        """
        now = self._now()
        ask = self.get(ask_id)
        payload = dict(ask.payload or {}) if ask else {}
        payload["decision"] = "approved" if approved else "rejected"
        new_status = ASK_FULFILLED if approved else ASK_REJECTED
        rc = execute_retry(
            self.db_path,
            "UPDATE goals SET status=?, completed_at=?, payload=? "
            "WHERE id=? AND kind=? AND status=? AND user_id=?",
            (new_status, now, json.dumps(payload), ask_id, KIND_ASK, ASK_OPEN, user_id),
        )
        if rc != 1:
            return (False, 0)
        self._event(ask_id, "ask_fulfilled" if approved else "ask_rejected", {})
        unblocked = 0
        if approved:
            for gid in (ask.payload or {}).get("blocks_goal_ids", []) if ask else []:
                # 2026-07-14 night-2: stamp the fulfillment onto the goal payload so
                # the retry prompt (context.build_goal_run_task) tells the run the
                # blocker was FIXED — without this the agent reads only the old
                # failure ledger and declares BLOCKED from memory without retrying.
                dep = self.get(gid)
                dep_payload = dict(dep.payload or {}) if dep else {}
                dep_payload["owner_unblocked"] = {"ts": now, "ask_id": ask_id}
                # T2.1 final-review Fix 1: an ask-fulfillment unblock is also an
                # owner reset — clear the stale block_kind (see unblock()'s
                # docstring for the full rationale). provider_requeues /
                # provider_retry_exhausted are untouched (only this ONE key is
                # popped) so the requeue-cap ledger survives the episode.
                dep_payload.pop("block_kind", None)
                rc2 = execute_retry(
                    self.db_path,
                    """UPDATE goals SET status='ready', consecutive_failures=0,
                          last_failure_error=NULL, claim_lock=NULL, claim_expires=NULL,
                          payload=?
                        WHERE id=? AND kind='goal' AND status='blocked' AND user_id=?""",
                    (json.dumps(dep_payload), gid, user_id),
                )
                if rc2 == 1:
                    self._event(gid, "unblocked_by_ask", {"ask_id": ask_id})
                    unblocked += 1
        return (True, unblocked)

    def _release_asks(self, goal_id: str, reason: str) -> None:
        """Close the owner asks a now-terminal goal was blocking. Fail-open.

        The tenant is read off the goal row, so every terminal transition can
        release its asks without threading a user_id through call sites that
        never had one. Bookkeeping — it must never fail the transition itself.
        """
        try:
            goal = self.get(goal_id)
            if goal is not None and goal.user_id:
                self.close_asks_for_goal(goal.user_id, goal_id, reason=reason)
        except Exception:
            logger.debug("ask release skipped for %s", goal_id, exc_info=True)

    def close_asks_for_goal(self, user_id: str, goal_id: str, *,
                            reason: str = "goal_resolved") -> int:
        """Release *goal_id* from every OPEN ask, closing any ask left with none.

        Called when a goal reaches a terminal state (done / cancelled): the owner
        no longer has a decision to make about it. An ask blocking SEVERAL goals
        stays open until the last one resolves — the need is still real for the
        others.

        Only OPEN asks move, and only this tenant's, so an owner-answered ask is
        never reopened or re-closed. Returns the number of asks closed.
        """
        if not (user_id and goal_id):
            return 0
        closed = 0
        for ask in self.asks(user_id=user_id, status=ASK_OPEN):
            payload = dict(ask.payload or {})
            blocks = list(payload.get("blocks_goal_ids") or [])
            if goal_id not in blocks:
                continue
            remaining = [g for g in blocks if g != goal_id]
            payload["blocks_goal_ids"] = remaining
            if remaining:
                # Still needed by another goal — record the release, stay open.
                execute_retry(
                    self.db_path,
                    "UPDATE goals SET payload=? WHERE id=? AND kind=? AND user_id=?",
                    (json.dumps(payload), ask.id, KIND_ASK, user_id),
                )
                continue
            if self._obsolete_ask(ask.id, user_id=user_id, reason=reason,
                                  payload=payload, event_extra={"goal_id": goal_id}):
                closed += 1
        return closed

    def obsolete_ask(self, ask_id: str, *, user_id: str,
                     reason: str = "resolved") -> bool:
        """Close one OPEN ask because the NEED went away on its own.

        Distinct from :meth:`decide_ask` on purpose: fulfilled/rejected record an
        OWNER decision, and marking a self-resolving need as "the owner fulfilled
        it" would be a false record. Tenant-scoped CAS on ``status='open'``, so an
        already-answered ask is never reopened or re-closed.
        """
        ask = self.get(ask_id)
        if ask is None or ask.kind != KIND_ASK or ask.user_id != user_id:
            return False
        return self._obsolete_ask(ask_id, user_id=user_id, reason=reason,
                                  payload=dict(ask.payload or {}))

    def _obsolete_ask(self, ask_id: str, *, user_id: str, reason: str,
                      payload: Dict[str, Any],
                      event_extra: Optional[Dict[str, Any]] = None) -> bool:
        payload = dict(payload)
        payload["decision"] = "obsolete"
        payload["obsolete_reason"] = reason
        rc = execute_retry(
            self.db_path,
            "UPDATE goals SET status=?, completed_at=?, payload=? "
            "WHERE id=? AND kind=? AND status=? AND user_id=?",
            (ASK_OBSOLETE, self._now(), json.dumps(payload), ask_id, KIND_ASK,
             ASK_OPEN, user_id),
        )
        if rc != 1:
            return False
        ev = {"reason": reason}
        ev.update(event_extra or {})
        self._event(ask_id, "ask_obsolete", ev)
        return True

    def fulfill_ask(self, ask_id: str, *, user_id: str) -> tuple:
        """Mark an ask fulfilled and flip its BLOCKED dependent goals back to ready.

        The unblock hop: each dependent goal that is currently ``blocked`` gets a
        clean failure counter and re-enters the dispatch queue. Tenant-scoped CAS.
        Returns ``(ok, unblocked_count)``. Thin wrapper over :meth:`decide_ask`
        (``approved=True``) — kept as its own method since it's the established
        public name (`polyrob owner fulfill`, Telegram ``/fulfill``).
        """
        return self.decide_ask(ask_id, user_id=user_id, approved=True)

    def consume_ask_grant(self, ask_id: str) -> bool:
        """Atomically consume a FULFILLED ask's one-shot grant flag (Task 9 / G-2).

        A ``tool_approval`` ask is created with ``payload.grant_consumed=false``
        (see :class:`tools.controller.approval_queue.OwnerQueueApprover`). Once
        it is approved (fulfilled) AFTER the requester already timed out and gave
        up, the NEXT identical request may redeem it exactly once. The CAS is a
        single atomic ``UPDATE ... WHERE id=? AND
        json_extract(payload, '$.grant_consumed')=0`` — the same atomic-claim
        shape ``modules/x402/invoicing.py::claim_wake`` uses (no
        read-modify-write race between two concurrent redeemers). Returns True
        for the caller that won the claim; False otherwise (already consumed,
        no such ask, or the payload doesn't carry the flag).

        Task 9b (2026-08-22): this used to do a string ``REPLACE`` on the
        machine-written ``"grant_consumed": false`` token, guarded by a
        ``LIKE`` match on the same spaced literal — the exact bug class Task 9
        fixed in ``modules/x402/invoicing.py`` (SQLite's ``json_set``
        re-serializes the WHOLE JSON blob COMPACTLY, which a spaced-literal
        match then silently stops matching). No ``json_set`` currently touches
        ``goals.payload`` anywhere in this codebase, so this was LATENT, not
        reachable — fixed anyway to remove the landmine before some future
        dedup/backfill routine (mirroring
        ``modules.database.x402_tables.dedupe_and_create_subscription_pending_unique_index``)
        adds one. ``json_set(..., json('true'))`` (NOT the bare number ``1``)
        keeps the stored shape a genuine JSON boolean, matching
        ``claim_wake``'s convention.
        """
        rc = execute_retry(
            self.db_path,
            """UPDATE goals SET payload = json_set(payload, '$.grant_consumed', json('true'))
                WHERE id=? AND kind=? AND json_extract(payload, '$.grant_consumed')=0""",
            (ask_id, KIND_ASK),
        )
        return rc == 1

    def objectives(self, *, user_id: str, status: Optional[str] = None) -> List[Goal]:
        sql = "SELECT * FROM goals WHERE kind=? AND user_id=?"
        params: List[Any] = [KIND_OBJECTIVE, user_id]
        if status is not None:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY priority DESC, created_at"
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def set_objective_status(self, objective_id: str, status: str,
                             *, user_id: Optional[str] = None) -> bool:
        if status not in _OBJECTIVE_STATUSES:
            raise ValueError(f"invalid objective status {status!r} (use {sorted(_OBJECTIVE_STATUSES)})")
        sql = "UPDATE goals SET status=? WHERE id=? AND kind=?"
        params: tuple = (status, objective_id, KIND_OBJECTIVE)
        if user_id is not None:
            sql += " AND user_id=?"; params = params + (user_id,)
        rc = execute_retry(self.db_path, sql, params)
        if rc == 1:
            self._event(objective_id, f"objective_{status}", {})
        return rc == 1

    def update_fields(self, goal_id: str, *, user_id: Optional[str] = None,
                      title: Optional[str] = None, body: Optional[str] = None,
                      priority: Optional[int] = None,
                      payload_patch: Optional[Dict[str, Any]] = None) -> bool:
        """Owner-edit of a non-terminal row. payload_patch is a shallow merge."""
        g = self.get(goal_id)
        if g is None or (user_id is not None and g.user_id != user_id):
            return False
        if g.status in ("done", "cancelled", "dropped"):
            return False
        sets, params = [], []
        if title is not None:
            sets.append("title=?"); params.append(title)
        if body is not None:
            sets.append("body=?"); params.append(body)
        if priority is not None:
            sets.append("priority=?"); params.append(int(priority))
        if payload_patch:
            merged = dict(g.payload or {}); merged.update(payload_patch)
            sets.append("payload=?"); params.append(json.dumps(merged))
        if not sets:
            return False
        params.append(goal_id)
        rc = execute_retry(self.db_path, f"UPDATE goals SET {', '.join(sets)} WHERE id=?",
                           tuple(params))
        if rc == 1:
            self._event(goal_id, "edited", {k: True for k in
                        ("title" if title is not None else "",
                         "body" if body is not None else "",
                         "priority" if priority is not None else "",
                         "payload" if payload_patch else "") if k})
        return rc == 1

    def children(self, parent_id: str, *, user_id: Optional[str] = None) -> List[Goal]:
        """T2.1 Task 5 hardening: ``user_id`` is an OPTIONAL tenant filter,
        default ``None`` (today's cross-tenant listing, unchanged for existing
        callers). Passing it restricts results to that tenant's own children —
        `children()` was not self-defending (a caller could enumerate another
        tenant's sub-goals by a known parent id)."""
        sql = "SELECT * FROM goals WHERE parent_id=?"
        params: List[Any] = [parent_id]
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        sql += " ORDER BY created_at"
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def objective_last_activity(self, user_id: str) -> Dict[str, float]:
        """Newest child-goal activity per objective, as a unix timestamp.

        "Activity" is ``started_at`` when the child has run and ``created_at`` when
        it has not — a queued-but-never-dispatched child still means the planner has
        already served that objective. Cancelled and dropped children do not count.
        An objective with no live child is ABSENT from the map; the caller reads
        absent as 0.0, which sorts it to the front of the starvation order.
        """
        rows = execute_retry(
            self.db_path,
            """SELECT parent_id AS k, MAX(COALESCE(started_at, created_at)) AS t
                 FROM goals
                WHERE kind='goal' AND user_id=? AND parent_id IS NOT NULL
                  AND status NOT IN ('cancelled','dropped')
                GROUP BY parent_id""",
            (user_id,),
            fetch="all",
        ) or []
        out: Dict[str, float] = {}
        for r in rows:
            try:
                key, t = r["k"], r["t"]
            except (KeyError, TypeError, IndexError):
                key, t = r[0], r[1]
            if key and t is not None:
                out[str(key)] = float(t)
        return out

    # --- planner bookkeeping keys (FIX 5: tenant-scoped) ---------------------
    # Every planner row used to key on the bare PLANNER_SENTINEL with no user_id
    # filter — unlike every other query in this file. On a multi-tenant board one
    # tenant's planner run reset EVERY tenant's backoff and its escalation dedup
    # suppressed another tenant's stall ask. Rows are now written under
    # "__planner__:<user_id>"; READS cover that key AND the bare legacy key, so an
    # upgrade neither crashes on the old rows nor silently resets a live streak to
    # zero (which would re-arm the very escalation storm the durable marker
    # exists to stop). Legacy rows can only LENGTHEN a streak — the safe
    # direction: more backoff, stickier dedup, never a new push.

    def _planner_key(self, user_id: Optional[str]) -> str:
        return f"{self.PLANNER_SENTINEL}:{user_id}" if user_id else self.PLANNER_SENTINEL

    def _planner_read_keys(self, user_id: Optional[str]) -> tuple:
        key = self._planner_key(user_id)
        return (key,) if key == self.PLANNER_SENTINEL else (key, self.PLANNER_SENTINEL)

    def last_planner_run_at(self, user_id: Optional[str] = None) -> Optional[float]:
        keys = self._planner_read_keys(user_id)
        placeholders = ",".join("?" for _ in keys)
        row = execute_retry(
            self.db_path,
            f"SELECT MAX(created_at) AS t FROM goal_events "
            f"WHERE goal_id IN ({placeholders}) AND kind='planner_run'",
            keys, fetch="one",
        )
        try:
            t = row["t"] if row else None
        except (KeyError, TypeError, IndexError):
            t = row[0] if row else None
        return float(t) if t else None

    def mark_planner_run(self, user_id: Optional[str] = None) -> None:
        self._event(self._planner_key(user_id), "planner_run", {})

    # --- planner outcome + stall escalation (durable) ------------------------
    # The dispatcher used to keep "consecutive empty planner runs" and "already
    # escalated this stall" as instance attributes. Prod 2026-08-29: the service
    # was restarted 14 times in 36 h (maintenance-loop 2-file deploys) and every
    # restart re-armed the once-per-stall owner push. These live in goal_events
    # under the planner sentinel, so a restart changes nothing.

    def mark_planner_outcome(self, *, queued: int, live: int = 0,
                             user_id: Optional[str] = None) -> None:
        """Record a planner run: how many goals it queued, and how many goals
        were still in flight on the board afterwards (``live``). Backoff keys on
        ``queued`` alone (the planner is not producing); the stall streak needs
        BOTH to be zero (nothing queued AND nothing running/waiting/ready)."""
        self._event(self._planner_key(user_id), "planner_outcome",
                    {"queued": int(max(0, queued)), "live": int(max(0, live))})

    def _planner_outcomes_desc(self, limit: int = 50,
                               user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        keys = self._planner_read_keys(user_id)
        placeholders = ",".join("?" for _ in keys)
        rows = execute_retry(
            self.db_path,
            f"SELECT payload, created_at FROM goal_events WHERE goal_id IN ({placeholders}) "
            f"AND kind='planner_outcome' ORDER BY id DESC LIMIT ?",
            (*keys, int(limit)), fetch="all") or []
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
                ts = float(r["created_at"])
            except (KeyError, TypeError, IndexError, ValueError):
                payload = json.loads(r[0] or "{}")
                ts = float(r[1])
            out.append({"queued": int(payload.get("queued") or 0),
                        "live": int(payload.get("live") or 0), "ts": ts})
        return out

    def consecutive_empty_planner_runs(self, user_id: Optional[str] = None) -> int:
        """Length of the trailing run of planner outcomes that queued nothing."""
        n = 0
        for o in self._planner_outcomes_desc(user_id=user_id):
            if o["queued"] > 0:
                break
            n += 1
        return n

    def consecutive_stall_runs(self, user_id: Optional[str] = None) -> int:
        """Trailing planner runs that queued nothing AND left no live goal."""
        n = 0
        for o in self._planner_outcomes_desc(user_id=user_id):
            if o["queued"] > 0 or o["live"] > 0:
                break
            n += 1
        return n

    def empty_streak_started_at(self, user_id: Optional[str] = None) -> Optional[float]:
        """Timestamp of the FIRST run of the current STALL streak (queued==0 and
        live==0), or ``None`` when the latest run queued something or left work
        on the board (no streak)."""
        started: Optional[float] = None
        for o in self._planner_outcomes_desc(user_id=user_id):
            if o["queued"] > 0 or o["live"] > 0:
                break
            started = o["ts"]
        return started

    def mark_stall_escalated(self, user_id: Optional[str] = None) -> None:
        """Durable "the owner was told about this stall" marker."""
        self._event(self._planner_key(user_id), "empty_pipeline_escalated", {})

    def stall_escalated_since(self, since: Optional[float],
                              user_id: Optional[str] = None) -> bool:
        """True if a stall escalation was recorded at or after *since* (the start
        of the current empty streak). ``since=None`` (no streak) is False."""
        if since is None:
            return False
        keys = self._planner_read_keys(user_id)
        placeholders = ",".join("?" for _ in keys)
        row = execute_retry(
            self.db_path,
            f"SELECT 1 AS x FROM goal_events WHERE goal_id IN ({placeholders}) "
            f"AND kind='empty_pipeline_escalated' AND created_at>=? LIMIT 1",
            (*keys, float(since)), fetch="one")
        return row is not None

    # --- internal ------------------------------------------------------------

    def _event(self, goal_id: str, kind: str, payload: Dict[str, Any]) -> None:
        try:
            execute_retry(
                self.db_path,
                "INSERT INTO goal_events (goal_id,kind,payload,created_at) VALUES (?,?,?,?)",
                (goal_id, kind, json.dumps(payload), self._now()),
            )
        except Exception:
            pass  # an audit-event write must never fail a state transition
