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
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional

from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)

# status lifecycle: triage -> waiting -> ready -> running -> {done | blocked} ; cancelled
# is terminal. `waiting` (T2.1) is a goal created with unresolved `depends_on` edges —
# it is NOT dispatchable (ready()'s filter excludes it with zero query changes) and
# flips to `ready` only when every prerequisite lands `done` (see deps_satisfied /
# the record_success completion sweep).
# The literals live in core.goal_vocab (core may not import agents; the status
# snapshot, the renderer, streams and goal_list all read the SAME spelling).
from core.goal_vocab import (  # noqa: E402,F401 — re-exported by name
    STATUS_TRIAGE, STATUS_WAITING, STATUS_READY, STATUS_RUNNING,
    STATUS_BLOCKED, STATUS_DONE, STATUS_CANCELLED,
    KIND_GOAL, KIND_OBJECTIVE, KIND_ASK,
    ASK_OPEN, ASK_FULFILLED, ASK_REJECTED, ASK_OBSOLETE,
    OBJ_ACTIVE, OBJ_PAUSED, OBJ_DROPPED, OBJ_DONE,
)


from agents.task.goals.board_types import (  # noqa: E402,F401 — re-exported by name
    READY_SCAN_LIMIT, _OBJECTIVE_STATUSES, DuplicateGoalError, SuppressedGoalError,
    _trigrams, title_similarity, ASK_AUTO_UNBLOCK, _ask_goal_keys, Goal,
)
from agents.task.goals.board_deps import BoardDepsMixin  # noqa: E402
from agents.task.goals.board_asks import BoardAsksMixin  # noqa: E402
from agents.task.goals.board_objectives import BoardObjectivesMixin  # noqa: E402
from agents.task.goals.board_planner import BoardPlannerMixin  # noqa: E402
from core.goal_vocab import normalize_title  # noqa: E402,F401 — the ONE title key


class GoalBoard(BoardDepsMixin, BoardAsksMixin, BoardObjectivesMixin,
                BoardPlannerMixin):
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
               actor: Optional[str] = None, rail_id: Optional[str] = None,
               tool_ceiling: Optional[List[str]] = None) -> Goal:
        """Write one row. ``actor`` names WHO is writing it (036 §3.3,
        ``core.tool_grants``): ``payload.tools`` must pass
        :func:`core.tool_grants.assert_grantable` for that actor —
        ``owner_seat`` / ``agent`` (+ ``tool_ceiling``) / ``rail`` (+ ``rail_id``)
        / ``none``. ``actor=None`` is the legacy/test path; every production call
        site names one (``tests/test_board_create_grant_ratchet.py``).
        A refusal raises :class:`core.tool_grants.GrantRefused` (a ``ValueError``)
        before any row is written."""
        from core.identity import is_anonymous
        if is_anonymous(user_id):
            raise ValueError("goal create requires a real (non-anonymous) user_id (tenant scope)")
        # An objective is allowed to be standing; it is not allowed to be infinite.
        if kind == KIND_GOAL:
            self._check_objective_budget(user_id, parent_id)
        from agents.task.constants import AutonomyConfig

        # 034 §3.5 read site 2: what the owner switched OFF is refused BEFORE,
        # and independent of, the dedup check — and `force` does not bypass it.
        if kind == KIND_GOAL:
            from core.goal_suppressions import blocking
            hit = blocking(self.db_path, user_id=user_id, title=title,
                           objective_id=parent_id,
                           stream_id=(payload or {}).get("stream"), now=self._now())
            if hit is not None:
                raise SuppressedGoalError(hit)

        # 036 §3.3: ONE predicate decides who may put a tool on a goal.
        if (payload or {}).get("tools"):
            from core.tool_grants import assert_grantable
            payload = dict(payload or {})
            payload["tools"] = assert_grantable(
                payload["tools"], actor=actor, rail_id=rail_id, user_id=user_id,
                db_path=self.db_path, ceiling=tool_ceiling)

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
                      completed_at=?
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
                   claim_lock=NULL, claim_expires=NULL
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

    def suppressions(self, *, user_id: str) -> list:
        """What the owner switched OFF (034), newest first. A read: never
        creates the table."""
        from core.goal_suppressions import active
        return active(self.db_path, user_id=user_id, now=self._now())

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
                    AND g.kind='goal'
                    ORDER BY (g.priority + ? * (SELECT COUNT(*) FROM goal_events e
                        WHERE e.goal_id = g.id AND e.kind='yielded')) DESC,
                        g.created_at LIMIT ?""",
                (int(yield_ageing), int(limit)), fetch="all",
            ) or []
            return self._drop_held([Goal.from_row(r) for r in rows])
        rows = execute_retry(
            self.db_path,
            """SELECT * FROM goals WHERE status='ready' AND claim_lock IS NULL AND kind='goal'
                ORDER BY priority DESC, created_at LIMIT ?""",
            (int(limit),), fetch="all",
        ) or []
        return self._drop_held([Goal.from_row(r) for r in rows])

    def ready_fair(self, *, limit: int, per_objective_cap: int = 0,
                   in_flight: Optional[Dict[str, int]] = None,
                   yield_ageing: int = 0) -> List[Goal]:
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
        for g in self.ready(limit=READY_SCAN_LIMIT, yield_ageing=yield_ageing):
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

    def create_objective(self, *, user_id: str, title: str, body: str = "",
                         priority: int = 5, force: bool = False,
                         payload: Optional[Dict[str, Any]] = None) -> Goal:
        """An objective is a standing, never-dispatched row goals attach to.

        ``payload`` may carry ``success_criteria`` (§7.3) so the planner measures
        against what the owner actually wants, not a self-set proxy.
        """
        return self.create(user_id=user_id, title=title, body=body, priority=priority,
                           kind=KIND_OBJECTIVE, status=OBJ_ACTIVE, force=force,
                           payload=payload, actor="none")

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
