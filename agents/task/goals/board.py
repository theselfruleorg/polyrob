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
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)

# status lifecycle: triage -> waiting -> ready -> running -> {done | blocked} ; cancelled
# is terminal. `waiting` (T2.1) is a goal created with unresolved `depends_on` edges —
# it is NOT dispatchable (ready()'s filter excludes it with zero query changes) and
# flips to `ready` only when every prerequisite lands `done` (see deps_satisfied /
# the record_success completion sweep). `paused` (080) is the OWNER's hold: never
# dispatched (ready() reads `ready` only), lifted by `/goal retry`.
# The literals live in core.goal_vocab (core may not import agents; the status
# snapshot, the renderer and goal_list all read the SAME spelling).
from core.goal_vocab import (  # noqa: E402,F401 — re-exported by name
    STATUS_TRIAGE, STATUS_WAITING, STATUS_READY, STATUS_RUNNING,
    STATUS_BLOCKED, STATUS_PAUSED, STATUS_DONE, STATUS_CANCELLED,
    KIND_GOAL, KIND_ASK,
    ASK_OPEN, ASK_FULFILLED, ASK_REJECTED, ASK_OBSOLETE,
)


from agents.task.goals.board_types import (  # noqa: E402,F401 — re-exported by name
    DuplicateGoalError, SuppressedGoalError,
    _trigrams, title_similarity, ASK_AUTO_UNBLOCK, _ask_goal_keys, Goal, clip_keep_end,
)
from agents.task.goals.board_deps import BoardDepsMixin  # noqa: E402
from agents.task.goals.board_asks import BoardAsksMixin  # noqa: E402
from agents.task.goals.machines import GOAL_MACHINE  # noqa: E402,F401 — 098 S1
from agents.task.goals.payload_keys import (  # noqa: E402
    append_capped, patch as _patch_payload, payload_expr)
from core.goal_vocab import normalize_title  # noqa: E402,F401 — the ONE title key

#: A goal a rate guard deferred (``defer_run``) is not claimable before its
#: ``payload.not_before`` (unix seconds). Bound parameter: now.
_NOT_HELD_SQL = ("(json_extract(g.payload, '$.not_before') IS NULL "
                 "OR json_extract(g.payload, '$.not_before') <= ?)")


class GoalBoard(BoardDepsMixin, BoardAsksMixin):
    """SQLite-backed durable goal store with atomic claim + circuit breaker."""

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
                    claim_token TEXT,
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
            # Serialize migrations so concurrent first opens cannot add twice.
            conn.execute("BEGIN IMMEDIATE")
            from core.sqlite_util import ensure_columns
            ensure_columns(conn, "goals", (("kind", "TEXT NOT NULL DEFAULT 'goal'"),
                                           ("claim_token", "TEXT")))
            conn.commit()
        finally:
            conn.close()

    # --- mutations -----------------------------------------------------------

    def create(self, *, user_id: str, title: str, body: str = "", priority: int = 5,
               parent_id: Optional[str] = None, max_retries: Optional[int] = None,
               payload: Optional[Dict[str, Any]] = None, status: str = STATUS_READY,
               kind: str = KIND_GOAL, force: bool = False,
               depends_on: Optional[List[str]] = None,
               actor: Optional[str] = None,
               tool_ceiling: Optional[List[str]] = None) -> Goal:
        """Write one row. ``actor`` names WHO is writing it (036 §3.3,
        ``core.tool_grants``): ``payload.tools`` must pass
        :func:`core.tool_grants.assert_grantable` for that actor —
        ``owner_seat`` / ``agent`` (+ ``tool_ceiling``) / ``none``. ``actor=None`` is the legacy/test path; every production call
        site names one (``tests/test_board_create_grant_ratchet.py``).
        A refusal raises :class:`core.tool_grants.GrantRefused` (a ``ValueError``)
        before any row is written."""
        from core.identity import is_anonymous
        if is_anonymous(user_id):
            raise ValueError("goal create requires a real (non-anonymous) user_id (tenant scope)")
        from agents.task.constants import AutonomyConfig

        # 034 §3.5 read site 2: what the owner switched OFF is refused BEFORE,
        # and independent of, the dedup check — and `force` does not bypass it.
        if kind == KIND_GOAL:
            from core.goal_suppressions import blocking
            hit = blocking(self.db_path, user_id=user_id, title=title, now=self._now())
            if hit is not None:
                raise SuppressedGoalError(hit)

        # 036 §3.3: ONE predicate decides who may put a tool on a goal.
        if (payload or {}).get("tools"):
            from core.tool_grants import assert_grantable
            payload = dict(payload or {})
            payload["tools"] = assert_grantable(
                payload["tools"], actor=actor, ceiling=tool_ceiling)

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
        if actor is not None:
            # 085 W8 A4: WHO wrote the row — the dispatch re-check reads it.
            effective_payload["actor"] = actor
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
        Each execution gets a new token, including retries by the same worker.
        """
        now = self._now()
        expires = now + max(1, int(ttl_seconds))
        token = uuid.uuid4().hex
        row = execute_retry(
            self.db_path,
            """UPDATE goals
                  SET status='running', claim_lock=?, claim_token=?, claim_expires=?,
                      started_at=COALESCE(started_at,?), last_heartbeat_at=?
                WHERE id=? AND status='ready' AND claim_lock IS NULL RETURNING *""",
            (worker, token, expires, now, now, goal_id), fetch="one",
        )
        if row is None:
            return None
        self._event(goal_id, "claimed", {"worker": worker})
        return Goal.from_row(row)

    def heartbeat(self, goal_id: str, worker: str, *, ttl_seconds: int,
                  claim_token: Optional[str] = None) -> bool:
        now = self._now()
        rc = execute_retry(
            self.db_path,
            """UPDATE goals SET last_heartbeat_at=?, claim_expires=?
                WHERE id=? AND claim_lock=? AND claim_token IS ? AND status='running'""",
            (now, now + max(1, int(ttl_seconds)), goal_id, worker, claim_token),
        )
        return rc == 1

    def record_success(self, goal_id: str, *, session_id: Optional[str] = None,
                       result: Optional[str] = None,
                       claim_token: Optional[str] = None) -> bool:
        """Complete only this claim; an absent token matches only legacy claims."""
        now = self._now()
        rc = execute_retry(
            self.db_path,
            """UPDATE goals
                  SET status='done', result=?, session_id=COALESCE(?,session_id),
                      consecutive_failures=0, claim_lock=NULL, claim_expires=NULL,
                      completed_at=?, payload=json_remove(payload, '$.run_deferrals')
                WHERE id=? AND status='running' AND claim_token IS ?""",
            (result, session_id, now, goal_id, claim_token),
        )
        if rc != 1:
            # Owner intervened (cancel/pause) while the run was in flight — their
            # decision wins. Keep the status; archive the late result as an event.
            self._event(goal_id, "stale_completion",
                        {"result": (result or "")[:500], "session_id": session_id})
            return False
        self._event(goal_id, "succeeded", {"session_id": session_id})
        self._sweep_dependents_on_completion(goal_id)
        self._release_asks(goal_id, "goal_done")
        return True

    def record_failure(self, goal_id: str, *, error: str,
                       session_id: Optional[str] = None,
                       claim_token: Optional[str] = None) -> Goal:
        """Record this claim's failure and breaker transition atomically.

        The token identifies an execution, even if the same worker reclaims the
        goal. Keep it on terminal rows to attribute later outcome processing.
        An omitted token matches only an unfenced legacy claim.
        """
        now = self._now()
        row = execute_retry(
            self.db_path,
            """UPDATE goals SET consecutive_failures=consecutive_failures+1,
                   status=CASE WHEN consecutive_failures+1>=max_retries
                               THEN 'blocked' ELSE 'ready' END,
                   completed_at=CASE WHEN consecutive_failures+1>=max_retries
                                     THEN ? ELSE completed_at END,
                   last_failure_error=?, session_id=COALESCE(?,session_id),
                   claim_lock=NULL, claim_expires=NULL,
                   payload=json_remove(payload, '$.run_deferrals')
                 WHERE id=? AND status='running' AND claim_token IS ? RETURNING *""",
            (now, error[:2000], session_id, goal_id, claim_token), fetch="one",
        )
        if row is None:
            g = self.get(goal_id)
            if g is None:
                raise KeyError(goal_id)
            self._event(goal_id, "stale_completion", {"error": error[:500]})
            return g
        g = Goal.from_row(row)
        tripped = g.status == STATUS_BLOCKED
        self._event(goal_id, "gave_up" if tripped else "failed",
                    {"failures": g.consecutive_failures, "error": error[:500]})
        if tripped:
            self._cascade_dep_failed(goal_id)
        self._append_attempt(goal_id, error=error, session_id=session_id)
        return g

    #: A rate guard may defer one goal this many times in a row; past it the run
    #: goes back through ``record_failure`` so a guard can never hold a goal forever.
    MAX_RUN_DEFERRALS = 3

    def defer_run(self, goal_id: str, *, retry_after_sec: int,
                  claim_token: Optional[str] = None, reason: str = "") -> Optional[Goal]:
        """Requeue a running goal that a rate guard cut short, with no failure counted.

        Prod 2026-10-14: the X post cooldown refused a goal run with
        ``retry_after_sec=3364``; the run was recorded as a failure, re-claimed a
        minute later into the same refusal, and the goal gave up in ten minutes.
        ``payload.not_before`` keeps ``ready()`` from claiming it before the guard
        reopens. Returns None (caller records a failure) for a stale claim or once
        the goal has been deferred :data:`MAX_RUN_DEFERRALS` times in a row (a
        recorded success or failure ends the row: both drop ``run_deferrals``)."""
        # AG-17/AG-21: the two keys change inside the status CAS (json_set) —
        # never a read-back write of the whole payload — and the deferral cap
        # is part of the guard, so two deferrals cannot both pass it.
        n_sql = "COALESCE(json_extract(payload, '$.run_deferrals'), 0)"
        row = execute_retry(
            self.db_path,
            f"""UPDATE goals SET status='ready', claim_lock=NULL, claim_expires=NULL,
                   payload=json_set(json_set(COALESCE(payload, '{{}}'),
                       '$.run_deferrals', {n_sql} + 1), '$.not_before', ?)
                 WHERE id=? AND status='running' AND claim_token IS ?
                   AND {n_sql} < ? RETURNING *""",
            (self._now() + max(1, int(retry_after_sec)), goal_id, claim_token,
             int(self.MAX_RUN_DEFERRALS)), fetch="one",
        )
        if row is None:
            return None
        g = Goal.from_row(row)
        self._event(goal_id, "deferred", {"retry_after_sec": int(retry_after_sec),
                                          "deferrals": int((g.payload or {}).get("run_deferrals") or 0),
                                          "reason": str(reason)[:300]})
        return g

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
        rows = execute_retry(
            self.db_path,
            "SELECT id, claim_lock, claim_expires FROM goals WHERE status='running' "
            "AND claim_expires IS NOT NULL AND claim_expires < ?",
            (now,), fetch="all",
        ) or []
        n = 0
        for r in rows:
            # AG-17: ONE statement per row, keyed on the exact claim read — the
            # failure count and the status move together, so a heartbeat that
            # extends the claim in between (or a second reclaimer) changes nothing.
            hit = execute_retry(
                self.db_path,
                """UPDATE goals SET consecutive_failures=consecutive_failures+1,
                       status=CASE WHEN consecutive_failures+1>=max_retries
                                   THEN 'blocked' ELSE 'ready' END,
                       completed_at=CASE WHEN consecutive_failures+1>=max_retries
                                         THEN ? ELSE completed_at END,
                       last_failure_error=CASE WHEN consecutive_failures+1>=max_retries
                           THEN 'reclaimed: worker crashed (stale claim)'
                           ELSE last_failure_error END,
                       claim_lock=NULL, claim_expires=NULL
                     WHERE id=? AND status='running' AND claim_lock IS ?
                       AND claim_expires IS ? AND claim_expires < ? RETURNING status""",
                (now, r["id"], r["claim_lock"], r["claim_expires"], now), fetch="one",
            )
            if hit is None:
                continue
            n += 1
            if hit["status"] == STATUS_BLOCKED:
                self._cascade_dep_failed(r["id"])
        return n

    def requeue_running_on_boot(self) -> int:
        """How many rows :meth:`requeue_running_on_boot_ids` requeued."""
        return len(self.requeue_running_on_boot_ids())

    def requeue_running_on_boot_ids(self) -> List[str]:
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

        Returns the requeued ids: each was cut by the restart, and the boot path
        gives each its terminal ``goal_run cut_by_restart`` row (116 W2.4).
        """
        from agents.task.goals.claim_liveness import claim_owner_alive
        now = self._now()
        rows = execute_retry(
            self.db_path,
            "SELECT id, claim_lock, claim_expires FROM goals "
            "WHERE status='running' AND kind='goal'",
            (), fetch="all",
        ) or []
        requeued: List[str] = []
        if not rows:
            return requeued
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
                requeued.append(gid)
                self._event(gid, "requeued_on_boot", {"reason": reason})
        return requeued

    def hold_running(self, *, worker: str, reason: str,
                     goal_ids: Optional[List[str]] = None,
                     reason_kind: str = "pause") -> List[str]:
        """031 owner pause: return THIS worker's running goals to ``ready`` with the
        claim cleared and NO failure increment — a pause is not the goal's fault.
        Scoped to ``claim_lock=worker`` so a second process's runs are untouched
        (it holds its own on its next tick).

        057 WS-C (A2): ``goal_ids`` scopes the hold further — a rail yield cancels
        only the runs holding the workspace, so it must return only THOSE rows to
        ``ready`` (returning a still-running goal would double-dispatch it). An
        EMPTY list holds nothing; ``None`` (the default) keeps the pause shape.
        ``reason_kind`` is ``pause`` or ``rail``; it rides in the ``held`` event's
        payload so "why was this row held" is queryable. ``held_by_pause`` is
        still emitted alongside for one release (its only reader today is
        ``tests/unit/agents/task/goals/test_board_hold.py``).
        """
        if goal_ids is not None and not goal_ids:
            return []
        sql = ("SELECT id FROM goals WHERE status='running' AND kind='goal' "
               "AND claim_lock=?")
        params: tuple = (worker,)
        if goal_ids is not None:
            marks = ",".join("?" for _ in goal_ids)
            sql += f" AND id IN ({marks})"
            params = params + tuple(str(g) for g in goal_ids)
        rows = execute_retry(self.db_path, sql, params, fetch="all") or []
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
                self._event(r["id"], "held",
                            {"reason": str(reason)[:200], "reason_kind": str(reason_kind)})
                # Legacy alias, kept for ONE release (grep before removing).
                self._event(r["id"], "held_by_pause", {"reason": str(reason)[:200]})
        return held

    def stamp_session(self, goal_id: str, session_id: Optional[str]) -> bool:
        """057 WS-C (B9): record the session a run used WITHOUT a terminal write.

        ``record_success``/``record_failure`` COALESCE the session id in, so a goal
        that was yielded (neither) had no session to resume into. Never clears an
        existing id."""
        if not session_id:
            return False
        return bool(execute_retry(
            self.db_path,
            "UPDATE goals SET session_id=? WHERE id=?", (str(session_id), goal_id)))

    def yield_count(self, goal_id: str) -> int:
        """057 WS-C (B11): how many times this goal was pre-empted for a rail.

        Read from the durable ``goal_events`` ledger rather than a payload counter,
        so it cannot drift from the events the status surface renders."""
        try:
            row = execute_retry(
                self.db_path,
                "SELECT COUNT(*) AS n FROM goal_events WHERE goal_id=? AND kind='yielded'",
                (goal_id,), fetch="one")
        except Exception:
            return 0
        if not row:
            return 0
        try:
            return int(row["n"])
        except (KeyError, TypeError, IndexError):
            return int(row[0])

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
        expr, eparams = payload_expr(remove=("block_kind",))
        rc = execute_retry(
            self.db_path,
            f"""UPDATE goals SET status='ready', consecutive_failures=0,
                  claim_lock=NULL, claim_expires=NULL, completed_at=NULL, payload={expr}
                WHERE id=? AND kind='goal' AND status='blocked' AND user_id=?""",
            (*eparams, goal_id, user_id),
        )
        if rc == 1:
            self._event(goal_id, "unblocked", {"rationale": str(rationale)[:500]})
            # Prod 2026-10-14: an unblock left the dispatcher's own ask OPEN and
            # ready() held the goal by it; the unblock answers that ask.
            self.release_auto_unblock_asks(goal_id)
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
        # Idempotence predicate (2026-07-23 validation): this UPDATE changes no
        # status, so WHERE status='blocked' alone let two concurrent sweeps both
        # get rc=1 and double-emit the event (the maintenance sweeps run before
        # the TickLock). Gating on the STORED kind makes the loser rc=0 — and
        # rejects its stale-payload write wholesale.
        # AG-21: only this key, and the stored kind is still the one read.
        rc = _patch_payload(
            self.db_path, goal_id, set_keys={"block_kind": block_kind},
            where_extra="status='blocked' AND json_extract(payload,'$.block_kind') IS ?",
            where_params=(existing,))
        if rc == 1:
            self._event(goal_id, "block_kind_set", {"block_kind": block_kind})
        return rc == 1

    def merge_payload(self, goal_id: str, updates: Dict[str, Any]) -> bool:
        """Merge ``updates`` into a row's JSON payload, preserving every other key.

        ``stamp_block_kind`` does this for exactly one key on exactly one status;
        this is the general form: ``json_set`` of exactly the keys in
        ``updates`` (AG-21) — a concurrent writer of ANY other key keeps it.
        """
        if not updates:
            return self.get(goal_id) is not None
        return _patch_payload(self.db_path, goal_id, set_keys=updates)

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
          blocked, else creation) ages out to ``cancelled`` (logged ``aged_out``,
          told to the owner by the stop sweep) and cascades to ``dep_failed`` dependents exactly like
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
                    # The next block names its own kind: a stale outage would hide it.
                    expr, eparams = payload_expr({"provider_requeues": requeues + 1},
                                                 remove=("block_kind",))
                    rc = execute_retry(
                        self.db_path,
                        f"""UPDATE goals SET status='ready', consecutive_failures=0,
                              claim_lock=NULL, claim_expires=NULL, completed_at=NULL,
                              payload={expr}
                            WHERE id=? AND status='blocked'
                              AND COALESCE(json_extract(payload,'$.provider_requeues'),0)=?""",
                        (*eparams, g.id, requeues),
                    )
                    if rc == 1:
                        n += 1
                        self._event(g.id, "provider_outage_retried",
                                    {"provider_retry_min": provider_retry_min,
                                     "requeues": requeues + 1})
                        self.release_auto_unblock_asks(g.id)
                    continue
                # Cap reached: stop self-healing, stamp ONCE, then fall through
                # below to the same terminal rail every other kind uses. The
                # stored-payload predicate makes "ONCE" hold under concurrent
                # sweeps too (2026-07-23 validation: the sweeps run before the
                # TickLock, and a payload-only UPDATE gave both writers rc=1 —
                # duplicate provider_retry_exhausted events).
                rc = _patch_payload(
                    self.db_path, g.id, set_keys={"provider_retry_exhausted": True},
                    where_extra="status='blocked' AND "
                                "json_extract(payload,'$.provider_retry_exhausted') IS NOT 1")
                if rc:
                    self._event(g.id, "provider_retry_exhausted", {"requeues": requeues})

            # needs_input / dep_failed / absent / unknown / requeue-exhausted
            # provider_outage: never auto-requeued, but ALL still subject to
            # the legacy terminal max-age cutoff (review finding #3).
            if blocked_at >= cutoff_legacy:
                continue
            # The stop sweep (goals/escalation.py) tells the owner from these keys.
            expr, eparams = payload_expr({"aged_out": True}, remove=("stop_told",))
            rc = execute_retry(
                self.db_path,
                f"""UPDATE goals SET status='cancelled', completed_at=?, payload={expr}
                    WHERE id=? AND status='blocked'""",
                (now, *eparams, g.id),
            )
            if rc == 1:
                n += 1
                self._event(g.id, "aged_out", {"max_age_days": max_age_days})
                self.close_asks_for_goal(g.user_id, g.id, reason="goal_aged_out")
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
            expr, eparams = payload_expr({"block_kind": "dep_failed"})
            rc = execute_retry(
                self.db_path,
                f"""UPDATE goals SET status='blocked', payload={expr}, completed_at=?
                    WHERE id=? AND kind='goal' AND status='waiting'""",
                (*eparams, self._now(), gid),
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
            # AG-21: a CAS on the one key — a concurrent writer of any other
            # payload key keeps its write.
            append_capped(self.db_path, goal_id, "attempts", {
                "ts": self._now(),
                # B9: head + tail — the detail a retry needs trails the error.
                "error": clip_keep_end(error, 600),
                "session_id": session_id,
            }, self._MAX_ATTEMPTS_KEPT)
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
        return _patch_payload(self.db_path, goal_id,
                              set_keys={"outcome": str(outcome)[:1000]})

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
        # kind guard: only a goal row is cancelled — an ask has its own statuses.
        sql = ("UPDATE goals SET status='cancelled', claim_lock=NULL, claim_token=NULL "
               "WHERE id=? AND kind='goal' AND status NOT IN ('done','cancelled')")
        params: tuple = (goal_id,)
        if user_id is not None:
            sql += " AND user_id=?"
            params = (goal_id, user_id)
        rc = execute_retry(self.db_path, sql, params)
        if rc == 1:
            self._event(goal_id, "cancelled", {})
            from agents.task.goals.run_cancel import cancel_local_run
            cancel_local_run(self.db_path, goal_id)
            self._cascade_dep_failed(goal_id)
            self._release_asks(goal_id, "goal_cancelled")
        return rc == 1

    def transition(self, goal_id: str, to: str, *, from_states=None,
                   user_id: Optional[str] = None, reset_failures: bool = False) -> bool:
        """Move a goal ``from_states -> to`` in ONE guarded write (098 S2, AG-17).

        ``from_states`` is the status the caller decided on (an owner verb passes
        its snapshot); ``None`` means every source :data:`GOAL_MACHINE` lists for
        ``to``. A row that moved since (a cancel from another seat) is a CAS miss
        -> ``False``, never a revival. The claim — lock, expiry AND token — is
        cleared, so a run that was ``running`` can neither heartbeat nor record a
        result after the owner moved it. ``user_id`` is the tenant guard."""
        extra, extra_params = "kind='goal'", ()
        if user_id is not None:
            extra, extra_params = "kind='goal' AND user_id=?", (user_id,)
        cols = {"claim_lock": None, "claim_expires": None, "claim_token": None}
        if reset_failures:
            cols.update(consecutive_failures=0, last_failure_error=None)
        from core.state.sql import cas_transition
        ok = cas_transition(self.db_path, "goals", key="id=?", key_params=(goal_id,),
                            machine=GOAL_MACHINE, to=to, from_states=from_states,
                            set_cols=cols, where_extra=extra, where_params=extra_params)
        if ok:
            self._event(goal_id, f"status_{to}", {})
            if to == STATUS_READY:
                self.release_auto_unblock_asks(goal_id)
            if to in (STATUS_PAUSED, STATUS_CANCELLED):
                from agents.task.goals.run_cancel import cancel_local_run
                cancel_local_run(self.db_path, goal_id)
        return ok

    def update_status(self, goal_id: str, new_status: str, *, reset_failures: bool = False,
                      user_id: Optional[str] = None, from_states=None) -> bool:
        """:meth:`transition` under its old name (every edge the table allows
        into ``new_status`` unless ``from_states`` narrows it)."""
        return self.transition(goal_id, new_status, from_states=from_states,
                               user_id=user_id, reset_failures=reset_failures)

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
                    limit: int = 30, newest: str = "created") -> List[Goal]:
        """This tenant's GOAL rows, newest first, optionally filtered by status.

        ``newest="finished"`` orders by when the goal ended (``completed_at``,
        else ``created_at``) — what a "recently finished" list means; a goal
        created long ago and finished today is at the top, not past the window.

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
        order = ("COALESCE(completed_at, created_at)" if newest == "finished"
                 else "created_at")
        sql += f" ORDER BY {order} DESC LIMIT ?"
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

    def suppressions(self, *, user_id: str) -> list:
        """What the owner switched OFF (034), newest first. A read: never
        creates the table."""
        from core.goal_suppressions import SCOPE_TITLE, active
        return active(self.db_path, user_id=user_id, now=self._now(), scope=SCOPE_TITLE)

    def ready(self, *, limit: int = 10, yield_ageing: int = 0) -> List[Goal]:
        """Ready goals across all tenants, highest priority first (dispatcher feed).

        057 WS-C (B11): ``yield_ageing`` > 0 adds ``yield_count x yield_ageing``
        to the ordering priority, so a goal that has been PRE-EMPTED repeatedly
        climbs instead of queueing behind fresh work forever (prod 2026-09-19:
        one goal yielded 6 times in a night). ``0`` (the default) keeps the
        original query byte-for-byte — the correlated count never runs."""
        if int(yield_ageing) > 0:
            rows = execute_retry(
                self.db_path,
                """SELECT * FROM goals g WHERE g.status='ready' AND g.claim_lock IS NULL
                    AND g.kind='goal' AND """ + _NOT_HELD_SQL + """
                    ORDER BY (g.priority + ? * (SELECT COUNT(*) FROM goal_events e
                        WHERE e.goal_id = g.id AND e.kind='yielded')) DESC,
                        g.created_at LIMIT ?""",
                (self._now(), int(yield_ageing), int(limit)), fetch="all",
            ) or []
            return self._drop_held([Goal.from_row(r) for r in rows])
        rows = execute_retry(
            self.db_path,
            """SELECT * FROM goals g WHERE status='ready' AND claim_lock IS NULL AND kind='goal'
                AND """ + _NOT_HELD_SQL + """
                ORDER BY priority DESC, created_at LIMIT ?""",
            (self._now(), int(limit)), fetch="all",
        ) or []
        return self._drop_held([Goal.from_row(r) for r in rows])

    def events(self, goal_id: str, *, last: Optional[int] = None) -> List[Dict[str, Any]]:
        """A goal's history, oldest first; ``last=N`` reads only the newest N rows."""
        if last is None:
            sql, args = "SELECT * FROM goal_events WHERE goal_id=? ORDER BY id", (goal_id,)
        else:
            sql = ("SELECT * FROM (SELECT * FROM goal_events WHERE goal_id=? "
                   "ORDER BY id DESC LIMIT ?) ORDER BY id")
            args = (goal_id, int(last))
        rows = execute_retry(self.db_path, sql, args, fetch="all") or []
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.get("payload") or "{}")
            out.append(d)
        return out

    def last_event_times(self, *, user_id: str) -> Dict[str, float]:
        """``{goal_id: newest goal_events.created_at}`` over this tenant's rows —
        the "last activity" of a board view, in one grouped read."""
        rows = execute_retry(
            self.db_path,
            "SELECT e.goal_id AS goal_id, MAX(e.created_at) AS ts FROM goal_events e "
            "JOIN goals g ON g.id = e.goal_id WHERE g.user_id=? GROUP BY e.goal_id",
            (user_id,), fetch="all",
        ) or []
        return {r["goal_id"]: float(r["ts"]) for r in rows if r["ts"] is not None}

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
            # AG-21: only the patched keys (json_set), never the whole document.
            expr, eparams = payload_expr(payload_patch)
            sets.append(f"payload={expr}"); params.extend(eparams)
        if not sets:
            return False
        params.append(goal_id)
        rc = execute_retry(
            self.db_path,
            f"UPDATE goals SET {', '.join(sets)} WHERE id=? "
            "AND status NOT IN ('done','cancelled','dropped')",
            tuple(params))
        if rc == 1:
            self._event(goal_id, "edited", {k: True for k in
                        ("title" if title is not None else "",
                         "body" if body is not None else "",
                         "priority" if priority is not None else "",
                         "payload" if payload_patch else "") if k})
        return rc == 1

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
