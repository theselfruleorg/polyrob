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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)

# status lifecycle: triage -> ready -> running -> {done | blocked} ; cancelled is terminal
STATUS_TRIAGE = "triage"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

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
               kind: str = KIND_GOAL, force: bool = False) -> Goal:
        from core.identity import is_anonymous
        if is_anonymous(user_id):
            raise ValueError("goal create requires a real (non-anonymous) user_id (tenant scope)")
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

        g = Goal(
            id=self._id(), user_id=user_id, title=title, body=body, kind=kind, status=status,
            priority=priority, parent_id=parent_id,
            max_retries=AutonomyConfig.goal_max_retries() if max_retries is None else max_retries,
            payload=payload or {}, created_at=self._now(),
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
        # 2) Trip the breaker for those that reached max_retries.
        execute_retry(
            self.db_path,
            f"""UPDATE goals SET status='blocked', claim_lock=NULL, claim_expires=NULL,
                   completed_at=?, last_failure_error='reclaimed: worker crashed (stale claim)'
                 WHERE {stale} AND consecutive_failures >= max_retries""",
            (now, now),
        )
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
        """
        rows = execute_retry(
            self.db_path,
            "SELECT id FROM goals WHERE status='running' AND kind='goal'",
            (), fetch="all",
        ) or []
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        n = 0
        for gid in ids:
            rc = execute_retry(
                self.db_path,
                """UPDATE goals SET status='ready', claim_lock=NULL, claim_expires=NULL
                    WHERE id=? AND status='running'""",
                (gid,),
            )
            if rc == 1:
                n += 1
                self._event(gid, "requeued_on_boot", {})
        return n

    def unblock(self, goal_id: str, *, user_id: str, rationale: str = "") -> bool:
        """§5.3: requeue a ``blocked`` goal with a rationale (symmetric to
        ``fulfill_ask``). Tenant-scoped; resets the breaker so the retry budget
        is fresh. Returns False for a non-blocked row or a wrong tenant."""
        rc = execute_retry(
            self.db_path,
            """UPDATE goals SET status='ready', consecutive_failures=0,
                  claim_lock=NULL, claim_expires=NULL, completed_at=NULL
                WHERE id=? AND kind='goal' AND status='blocked' AND user_id=?""",
            (goal_id, user_id),
        )
        if rc == 1:
            self._event(goal_id, "unblocked", {"rationale": str(rationale)[:500]})
        return rc == 1

    def age_out_blocked(self, *, max_age_days: int = 14) -> int:
        """§5.3: age ancient ``blocked`` goals out VISIBLY (→ cancelled, logged)
        instead of letting them rot as permanent planner context. The age is
        measured from when the goal blocked (completed_at) else creation."""
        if max_age_days <= 0:
            return 0
        cutoff = self._now() - max_age_days * 86400
        rows = execute_retry(
            self.db_path,
            """SELECT id FROM goals
                WHERE status='blocked' AND kind='goal'
                  AND COALESCE(completed_at, created_at) < ?""",
            (cutoff,), fetch="all",
        ) or []
        n = 0
        now = self._now()
        for r in rows:
            gid = r["id"]
            rc = execute_retry(
                self.db_path,
                """UPDATE goals SET status='cancelled', completed_at=?
                    WHERE id=? AND status='blocked'""",
                (now, gid),
            )
            if rc == 1:
                n += 1
                self._event(gid, "aged_out", {"max_age_days": max_age_days})
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
        return rc == 1

    def update_status(self, goal_id: str, new_status: str, *, reset_failures: bool = False) -> bool:
        """Update goal status, optionally resetting failure counters."""
        sets = ["status=?", "claim_lock=NULL", "claim_expires=NULL"]
        params: List[Any] = [new_status, goal_id]
        if reset_failures:
            sets.append("consecutive_failures=0")
            sets.append("last_failure_error=NULL")
        sql = f"UPDATE goals SET {', '.join(sets)} WHERE id=?"
        rc = execute_retry(self.db_path, sql, tuple(params))
        if rc == 1:
            self._event(goal_id, f"status_{new_status}", {})
        return rc == 1

    # --- queries -------------------------------------------------------------

    def get(self, goal_id: str) -> Optional[Goal]:
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

    def ready(self, *, limit: int = 10) -> List[Goal]:
        """Ready goals across all tenants, highest priority first (dispatcher feed)."""
        rows = execute_retry(
            self.db_path,
            """SELECT * FROM goals WHERE status='ready' AND claim_lock IS NULL AND kind='goal'
                ORDER BY priority DESC, created_at LIMIT ?""",
            (int(limit),), fetch="all",
        ) or []
        return [Goal.from_row(r) for r in rows]

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
        string ``REPLACE`` guarded by a ``LIKE`` match on the literal
        ``json.dumps`` token — the same atomic-claim shape
        ``modules/x402/invoicing.py::claim_wake`` uses (no read-modify-write
        race between two concurrent redeemers). Returns True for the caller that
        won the claim; False otherwise (already consumed, no such ask, or the
        payload doesn't carry the flag).
        """
        rc = execute_retry(
            self.db_path,
            """UPDATE goals SET payload = REPLACE(payload, '"grant_consumed": false',
                                                    '"grant_consumed": true')
                WHERE id=? AND kind=? AND payload LIKE '%"grant_consumed": false%'""",
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

    def children(self, parent_id: str) -> List[Goal]:
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM goals WHERE parent_id=? ORDER BY created_at",
            (parent_id,), fetch="all",
        ) or []
        return [Goal.from_row(r) for r in rows]

    def last_planner_run_at(self) -> Optional[float]:
        row = execute_retry(
            self.db_path,
            "SELECT MAX(created_at) AS t FROM goal_events WHERE goal_id=? AND kind='planner_run'",
            (self.PLANNER_SENTINEL,), fetch="one",
        )
        try:
            t = row["t"] if row else None
        except (KeyError, TypeError, IndexError):
            t = row[0] if row else None
        return float(t) if t else None

    def mark_planner_run(self) -> None:
        self._event(self.PLANNER_SENTINEL, "planner_run", {})

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
