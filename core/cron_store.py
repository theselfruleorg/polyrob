"""The cron job store (U1 phase 2): the SQLite ``cron_jobs`` table, the job
machine (098 S1, MO-09) and the run lease.

Moved from ``cron/jobs.py`` (it re-exports the store) and ``cron/job_machine.py`` so
the tools tier (the ``cronjob`` tool) reads the store without importing ``cron``.
It imports nothing above core: the side tables a job takes with it (the wake-gate
baseline, the remedy streak) are dropped here by plain SQL, and the reclaimed-run
event is emitted here (:func:`emit_cron_run_event`; ``cron.runner._cron_ev`` wraps it).

A single ``cron_jobs`` table holding durable schedule state. Uses WAL mode with a
jittered retry on write contention. The store is intentionally schedule-agnostic:
callers compute ``next_run_at`` with :mod:`core.schedule` and hand it in.

The job machine: ``cron_jobs.status`` is the state; ``enabled`` is derived from it
(0 for ``done``/``failed``/``cancelled``) and written only beside a status move. A
``running`` row names its owner (``run_owner`` = the claiming process's identity,
098 S5) and a lease (``lease_until``, unix seconds): the reaper takes a row only
when its lease ran out or its owner process is provably gone, so a stolen tick
lock can no longer reset a live run and run it twice.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.sqlite_util import execute_retry, init_schema
from core.state.machine import StateMachine

logger = logging.getLogger(__name__)

#: The hard ceiling on one run's cap (seconds); floors only raise a cap up to it.
#: Moved from ``cron/scheduler.py`` (the scheduler re-imports it).
RUN_CAP_CEILING_SECONDS = 1800

CRON_JOB_MACHINE = StateMachine(
    name="cron_job",
    states=frozenset({"scheduled", "running", "paused", "done", "failed", "cancelled"}),
    transitions={
        "scheduled": frozenset({"running", "paused", "cancelled"}),
        "running": frozenset({"scheduled", "done", "failed", "paused", "cancelled"}),
        "paused": frozenset({"scheduled", "cancelled"}),
        "done": frozenset({"cancelled"}),
        "failed": frozenset({"cancelled"}),
    },
    terminal=frozenset({"cancelled"}),
    initial=frozenset({"scheduled", "paused"}),
)

#: Slack past the run's own cap before a lease counts as expired.
LEASE_SLACK_SECONDS = 120


def run_owner() -> str:
    from core.state.proc import process_identity
    return process_identity().encode()


def lease_is_live(owner: Optional[str], lease_until: Optional[float],
                  now: Optional[float] = None) -> Optional[bool]:
    """True = a live run holds the row; False = provably orphaned (lease out,
    or owner process gone); None = a legacy row with no lease (only the tick
    lock can judge it)."""
    if lease_until is None and not owner:
        return None
    now = time.time() if now is None else now
    if lease_until is not None and float(lease_until) < now:
        return False
    if owner:
        from core.state.proc import ProcIdentity, is_alive
        ident = ProcIdentity.decode(owner)
        if ident is not None and not is_alive(ident):
            return False
    return True


def drop_wake_baselines(db_path: str, *, job_id: Optional[str] = None) -> None:
    """Forget the wake-gate baseline of *job_id* (resume/cancel), or of every job
    no longer in ``cron_jobs`` (after a prune). A missing table is a no-op."""
    try:
        if job_id is not None:
            execute_retry(db_path, "DELETE FROM wake_gate WHERE job_id=?", (job_id,))
        else:
            execute_retry(db_path, "DELETE FROM wake_gate WHERE job_id NOT IN "
                                   "(SELECT id FROM cron_jobs)")
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower():
            raise


def drop_remedy_streaks(db_path: str, *, job_id: Optional[str] = None) -> None:
    """MO-12: a cancelled/pruned job takes its streak row with it
    (``job_id=None``: every row whose job is gone). A missing table is a no-op."""
    try:
        if job_id is not None:
            execute_retry(db_path, "DELETE FROM remedy_streak WHERE job_id=?", (job_id,))
        else:
            execute_retry(db_path, "DELETE FROM remedy_streak WHERE job_id NOT IN "
                                   "(SELECT id FROM cron_jobs)")
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower():
            raise


def emit_cron_run_event(job, outcome: str, reason: Optional[str] = None, *,
                        session_id: Optional[str] = None, **extra) -> None:
    """Emit a cron_run event to the durable event log (fail-open). Makes cron
    lifecycle queryable in the uniform autonomy/governance stream, not just the
    episodes table (telemetry audit 2026-07-04). A run that started a session
    carries its ``session_id`` in the row's own column (2026-10-14 review D-4).
    ``cron.runner._cron_ev`` calls this."""
    from core.event_log import emit, event_log_enabled
    if not event_log_enabled():
        return
    try:
        from core.cron_expect import verified_attrs  # 108 W5
        extra.update(verified_attrs(job, outcome, session_id))
    except Exception:
        logger.debug("cron_run verified attrs skipped (job %s)",
                     getattr(job, "id", None), exc_info=True)
    emit("cron_run", source="cron", user_id=getattr(job, "user_id", ""),
         session_id=str(session_id or ""),
         attrs={"job_id": getattr(job, "id", None), "outcome": outcome, "reason": reason,
                **extra})


#: Store paths whose dead state ``CronJobStore._clear_dead_state`` already cleared.
_CLEARED_STORES: set = set()


@dataclass
class CronJob:
    id: str
    task: str
    schedule_spec: str
    user_id: str
    next_run_at: Optional[datetime]
    one_shot: bool = False
    # DORMANT (ME-D2): never consumed by the runner; retained to avoid a schema
    # migration. Wire or drop in a dedicated proposal. Not settable via
    # CronService.schedule — always takes this default.
    skip_memory: bool = True
    #: 057 WS-C (B12): the fleet default is an env row (CRON_DEFAULT_MAX_DURATION_SEC)
    #: rather than a literal in two files. A per-job cap still wins.
    max_duration_seconds: int = field(default_factory=lambda: default_max_duration_sec())
    payload: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    status: str = "scheduled"  # scheduled|running|paused|done|failed|cancelled
    last_run_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    #: NOT stored as a column: how THIS run ended, set by the runner/scheduler
    #: on the claimed object — ``{"outcome": "failed", "reason": "stopped at the
    #: step limit (150/150)"}`` — and written to ``payload.last_outcome``.
    run_note: Optional[Dict[str, str]] = field(default=None, compare=False, repr=False)
    #: NOT stored as a column: how late THIS run started, in seconds; 0 when on time.
    late_seconds: float = field(default=0, compare=False, repr=False)
    #: NOT a stored field of the job: the lease THIS claim holds (``lease_until``
    #: from :meth:`CronJobStore.claim_for_run`). The run end is a CAS on it, so
    #: a run whose lease ran out and was reclaimed can never end the run that
    #: took the row after it (098 review).
    claim_lease: Optional[float] = field(default=None, compare=False, repr=False)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


#: 056 WS5: the priority class a job declares in ``payload.priority``. ``money``
#: marks the treasury rails (e.g. exit / safety / watcher / scout / a recurring swap); a due
#: money job pre-empts a running board goal (GOAL_YIELD_FOR_MONEY_RAIL).
CRON_PRIORITY_MONEY = "money"
#: 103: the head of the run's own report, so a reader takes the real cause from it.
LAST_REPORT_MAX_CHARS = 300


def default_max_duration_sec() -> int:
    """The per-run hard cap a job gets when its creator sets none (057 WS-C B12).

    ONE home: ``CRON_DEFAULT_MAX_DURATION_SEC`` via the config-policy accessor,
    read at access time so an env change needs no code edit. Fail-open to 600 —
    the value this was a literal for."""
    try:
        from core.config_policy import AutonomyConfig
        val = int(AutonomyConfig.cron_default_max_duration_sec())
        return val if val > 0 else 600
    except Exception:
        return 600


def money_job_class(job) -> Optional[bool]:
    """Whether *job* is money-class: ``core.money.classify.is_money_class`` over its
    payload (``priority=money`` OR a toolset/rig holding a money tool — review B-1).
    None = the class could not be read (with a warning)."""
    try:
        import core.skills.catalog  # noqa: F401 — 112: registers the money-skill probe; at CALL time (an import-time catalog build refuses pack policy rows)
        from core.money.classify import money_class_strict
        return bool(money_class_strict(getattr(job, "payload", None) or {}))
    except Exception:
        logger.warning("cron job %s: money class unknown", getattr(job, "id", "?"),
                       exc_info=True)
        return None


def is_money_job(job, *, unknown: bool = False) -> bool:
    """:func:`money_job_class` as a bool: a class that could not be read answers
    *unknown* (a refusal passes True)."""
    cls = money_job_class(job)
    return unknown if cls is None else cls


def job_preempts(job) -> bool:
    """Whether this job may PRE-EMPT a running board goal (057 WS-C B7).

    Priority is not pre-emption. ``payload.priority`` orders jobs within a tick;
    ``payload.preempts`` decides whether a due job is allowed to stop work that
    is already running. Prod's SAFETY and WATCHER rails are read-only and were
    money-class, so they caused 5 of the 17 yields on 2026-09-19 for nothing.

    Back-compat: a job that declares no ``preempts`` key answers ``is_money_job``
    — every existing row behaves exactly as before.
    """
    try:
        payload = getattr(job, "payload", None) or {}
        if "preempts" in payload:
            from core.env import parse_bool
            val = payload.get("preempts")
            if isinstance(val, bool):
                return val
            return bool(parse_bool(str(val), False))
        return is_money_job(job)
    except Exception:
        return False


_CRON_SCHEMA = """
            CREATE TABLE IF NOT EXISTS cron_jobs (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                schedule_spec TEXT NOT NULL,
                user_id TEXT NOT NULL,
                next_run_at TEXT,
                one_shot INTEGER NOT NULL DEFAULT 0,
                -- DORMANT (ME-D2): never consumed by the runner; retained to avoid
                -- a schema migration. Wire or drop in a dedicated proposal.
                skip_memory INTEGER NOT NULL DEFAULT 1,
                max_duration_seconds INTEGER NOT NULL DEFAULT 600,
                payload TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'scheduled',
                last_run_at TEXT,
                created_at TEXT NOT NULL
            )
"""
_CRON_COLUMNS = {"cron_jobs": (("run_owner", "TEXT"), ("lease_until", "REAL"))}


class CronJobStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()
        # NOTE: crash-orphaned 'running' jobs are reclaimed by the scheduler at the
        # start of each tick *under the held TickLock* (see CronScheduler._run_due),
        # NOT here. Calling reclaim in __init__ ran without the lock, so a second
        # CronJobStore built mid-tick (e.g. by the agent-facing CronJobTool, or
        # another worker's lifespan under UVICORN_WORKERS>1) would reset a genuinely
        # live job back to 'scheduled' and cause it to run twice.

    def _init_schema(self) -> None:
        # 125 S16 (B-H3): no BEGIN IMMEDIATE — a second store opens at once while
        # another process holds the file. MO-09: the run lease (owner process +
        # expiry) beside the status.
        init_schema(self.db_path, _CRON_SCHEMA, columns=_CRON_COLUMNS)
        self._clear_dead_state()

    def _clear_dead_state(self) -> None:
        """Once per process per store: a 'done' or 'failed' row written before the
        terminal write disabled it is disabled now, and the payload keys of the deleted
        authorship model (``authored_by``, ``adopted_skills_digest``) are
        dropped. Idempotent; fail-open (a busy store retries next process)."""
        if self.db_path in _CLEARED_STORES:
            return
        try:
            execute_retry(self.db_path,
                          "UPDATE cron_jobs SET enabled=0 "
                          "WHERE status IN ('done','failed','cancelled') AND enabled=1")
            # MO-09: a live status with enabled=0 was a cancel that a later
            # unguarded write moved back — it is cancelled.
            execute_retry(self.db_path,
                          "UPDATE cron_jobs SET status='cancelled' "
                          "WHERE status IN ('scheduled','paused') AND enabled=0")
            execute_retry(
                self.db_path,
                "UPDATE cron_jobs SET payload=json_remove(payload, '$.authored_by', "
                "'$.adopted_skills_digest') WHERE json_valid(payload) AND ("
                "json_type(payload, '$.authored_by') IS NOT NULL OR "
                "json_type(payload, '$.adopted_skills_digest') IS NOT NULL)")
            _CLEARED_STORES.add(self.db_path)
        except Exception:
            logger.debug("cron store dead-state clear failed for %s", self.db_path,
                         exc_info=True)

    # --- row mapping ---------------------------------------------------------

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> CronJob:
        return CronJob(
            id=row["id"], task=row["task"], schedule_spec=row["schedule_spec"],
            user_id=row["user_id"], next_run_at=_parse(row["next_run_at"]),
            one_shot=bool(row["one_shot"]), skip_memory=bool(row["skip_memory"]),
            max_duration_seconds=row["max_duration_seconds"],
            payload=json.loads(row["payload"] or "{}"),
            enabled=bool(row["enabled"]), status=row["status"],
            last_run_at=_parse(row["last_run_at"]), created_at=_parse(row["created_at"]),
        )

    # --- CRUD ----------------------------------------------------------------

    def add(self, job: CronJob) -> CronJob:
        created = job.created_at or datetime(2026, 1, 1)  # caller may override
        execute_retry(
            self.db_path,
            """
            INSERT INTO cron_jobs (id, task, schedule_spec, user_id, next_run_at,
                one_shot, skip_memory, max_duration_seconds, payload, enabled, status,
                last_run_at, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job.id, job.task, job.schedule_spec, job.user_id, _iso(job.next_run_at),
                int(job.one_shot), int(job.skip_memory), job.max_duration_seconds,
                json.dumps(job.payload), int(job.enabled), job.status,
                _iso(job.last_run_at), _iso(created),
            ),
        )
        job.created_at = created
        return job

    def get(self, job_id: str, *, user_id: Optional[str] = None) -> Optional[CronJob]:
        sql = "SELECT * FROM cron_jobs WHERE id=?"
        params: tuple = (job_id,)
        if user_id is not None:
            sql += " AND user_id=?"
            params = (job_id, user_id)
        row = execute_retry(self.db_path, sql, params, fetch="one")
        return self._row_to_job(row) if row else None

    def list(self, user_id: Optional[str] = None, enabled_only: bool = False) -> List[CronJob]:
        sql = "SELECT * FROM cron_jobs"
        clauses, params = [], []
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if enabled_only:
            clauses.append("enabled=1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all")
        return [self._row_to_job(r) for r in rows]

    def due(self, now: datetime) -> List[CronJob]:
        rows = execute_retry(
            self.db_path,
            """
            SELECT * FROM cron_jobs
            WHERE enabled=1 AND status IN ('scheduled')
              AND next_run_at IS NOT NULL AND next_run_at <= ?
            ORDER BY next_run_at
            """,
            (_iso(now),), fetch="all",
        )
        return [self._row_to_job(r) for r in rows]

    def update_after_run(self, job_id: str, *, last_run_at: datetime,
                         next_run_at: Optional[datetime], status: str,
                         expected: Optional[CronJob] = None) -> None:
        if expected is not None:
            # Compare and write in ONE statement: completion must not undo a
            # schedule edit or cancellation that landed during the run.
            unchanged = "schedule_spec=? AND one_shot=? AND next_run_at IS ?"
            match = (expected.schedule_spec, int(expected.one_shot), _iso(expected.next_run_at))
            # 098 review: only the claim that holds the lease ends the run.
            lease, lease_params = "", ()
            if expected.claim_lease is not None:
                lease = " AND run_owner IS ? AND lease_until IS ?"
                lease_params = (run_owner(), expected.claim_lease)
            # A job that reaches a terminal state ('done' or 'failed') is
            # disabled in the same write: nothing will run it again, and
            # enabled=1 kept its prose in the live rail readers (rules sweep,
            # status rail prose).
            execute_retry(
                self.db_path,
                f"""UPDATE cron_jobs SET last_run_at=?,
                    next_run_at=CASE WHEN {unchanged} THEN ? ELSE next_run_at END,
                    enabled=CASE WHEN {unchanged} AND ? IN ('done','failed') THEN 0 ELSE 1 END,
                    status=CASE WHEN {unchanged} THEN ? ELSE 'scheduled' END,
                    run_owner=NULL, lease_until=NULL
                    WHERE id=? AND enabled=1 AND status='running'{lease}""",
                (_iso(last_run_at), *match, _iso(next_run_at), *match, status,
                 *match, status, job_id, *lease_params),
            )
            return
        # MO-09: only the run ends a run — never a cancel or a pause.
        execute_retry(
            self.db_path,
            "UPDATE cron_jobs SET last_run_at=?, next_run_at=?, status=?, "
            "enabled=CASE WHEN ? IN ('done','failed') THEN 0 ELSE enabled END, "
            "run_owner=NULL, lease_until=NULL WHERE id=? AND status='running'",
            (_iso(last_run_at), _iso(next_run_at), status, status, job_id),
        )

    def run_now(self, job_id: str, now: datetime) -> bool:
        """Pull an enabled, 'scheduled' job's ``next_run_at`` to *now* so the
        next tick runs it. A CAS on status so a 'running'/'done'/'cancelled'
        row is never touched; returns True iff the row moved. Used by the owner
        queue (2026-09-18): approving an ask a cron run raised re-arms the job,
        because the grant can only be redeemed by a genuine cron turn."""
        from core.cron_rearm import rearm_job
        return rearm_job(self.db_path, job_id, now)

    def set_status(self, job_id: str, status: str) -> bool:
        """Scheduler-internal: a run this process holds goes back (``running ->
        scheduled`` after a pause cut or a shutdown requeue). MO-09: a CAS on
        ``running`` held by THIS process (or a legacy row with no owner) — a
        job the owner paused or cancelled mid-run keeps the owner's state."""
        from core.state.sql import cas_transition
        return cas_transition(
            self.db_path, "cron_jobs", key="id=?", key_params=(job_id,),
            machine=CRON_JOB_MACHINE, to=status, from_states=("running",),
            set_cols={"run_owner": None, "lease_until": None},
            where_extra="enabled=1 AND (run_owner IS NULL OR run_owner=?)",
            where_params=(run_owner(),))

    # --- owner job state (080 WS-2) -------------------------------------------
    #
    # A paused job is the job's OWN state, not a rule elsewhere: status
    # 'paused', no next run (nothing will run it, so nothing is shown), and
    # ``payload.paused`` names who paused it and when. ``due``/``claim_for_run``
    # only take 'scheduled' rows, so the ticker never sees a paused job.

    def pause(self, job_id: str, *, user_id: Optional[str], note: Dict[str, Any]) -> bool:
        """'scheduled'/'running' -> 'paused', next run cleared, ``payload.paused``
        = *note*. A CAS on status; True iff the row moved."""
        # A cut not yet settled goes with the pause: the run it names is long over
        # when the owner resumes, and settling it then would skip the fresh slot.
        sql = ("UPDATE cron_jobs SET status='paused', next_run_at=NULL, "
               "payload=json_set(json_remove(payload, '$.cut_run'), '$.paused', json(?)) "
               "WHERE id=? AND enabled=1 AND status IN ('scheduled','running')")
        params: tuple = (json.dumps(note), job_id)
        if user_id is not None:
            sql += " AND user_id=?"
            params = params + (user_id,)
        return bool(execute_retry(self.db_path, sql, params))

    def take_over_pause(self, job_id: str, *, user_id: Optional[str],
                        note: Dict[str, Any], from_vias: Tuple[str, ...]) -> bool:
        """Rewrite the pause note of a job that is ALREADY paused, only when its
        note's ``via`` is one of *from_vias* (a weaker hand). True iff it moved."""
        if not from_vias:
            return False
        marks = ",".join("?" for _ in from_vias)
        sql = ("UPDATE cron_jobs SET payload=json_set(payload, '$.paused', json(?)) "
               "WHERE id=? AND enabled=1 AND status='paused' "
               f"AND json_extract(payload, '$.paused.via') IN ({marks})")
        params: tuple = (json.dumps(note), job_id) + tuple(from_vias)
        if user_id is not None:
            sql += " AND user_id=?"
            params = params + (user_id,)
        return bool(execute_retry(self.db_path, sql, params))

    def resume(self, job_id: str, *, user_id: Optional[str],
               next_run_at: datetime, note_via: Optional[str] = None,
               keep_streak: bool = False) -> bool:
        """'paused' -> 'scheduled' at *next_run_at*; drops ``payload.paused`` and the
        failure streak, so the job gets a full new count; ``last_outcome.resumed``
        lets the first failure of that count be told again. *note_via* set = move
        only while the pause note still names that hand (the note the caller judged).
        *keep_streak* = the agent's own resume: the failure count stays, so a pause and
        resume of its own never resets the failure limit (only the owner gives a new count)."""
        drop = "'$.paused', '$.cut_run'" if keep_streak else \
            "'$.paused', '$.last_outcome.streak', '$.cut_run'"
        sql = ("UPDATE cron_jobs SET status='scheduled', next_run_at=?, "
               f"payload=json_set(json_remove(payload, {drop}), '$.last_outcome.resumed', 1) "
               "WHERE id=? AND enabled=1 AND status='paused'")
        params: tuple = (_iso(next_run_at), job_id)
        if note_via is not None:
            sql += " AND json_extract(payload, '$.paused.via') = ?"
            params = params + (note_via,)
        if user_id is not None:
            sql += " AND user_id=?"
            params = params + (user_id,)
        ok = bool(execute_retry(self.db_path, sql, params))
        if ok:
            self._drop_side_rows(job_id, streak=False)  # MO-11: a resumed job runs once
        return ok

    def _drop_side_rows(self, job_id: Optional[str], *, streak: bool = True) -> None:
        """MO-11/MO-12: the job's wake-gate baseline (and its remedy streak)
        go with it — ``job_id=None`` drops every row whose job is gone."""
        try:
            drop_wake_baselines(self.db_path, job_id=job_id)
            if streak:
                drop_remedy_streaks(self.db_path, job_id=job_id)
        except Exception:
            logger.debug("cron side rows not dropped for %s", job_id, exc_info=True)

    def record_outcome(self, job_id: str, outcome: str, at: datetime,
                       reason: Optional[str] = None,
                       report: Optional[str] = None,
                       streak: int = 0, late_by: float = 0) -> None:
        """Store the last run's outcome on the row (``payload.last_outcome``):
        recurring jobs go back to 'scheduled' after every run, so the status
        alone never says how the last run ended. *reason* (why a run did not
        finish, e.g. "stopped at the step limit (150/150)") rides with it.
        *streak* counts the failed runs in a row. Merges; never replaces."""
        rec = {"outcome": str(outcome), "at": _iso(at)}
        if streak:
            rec["streak"] = int(streak)
        if late_by:
            rec["late_by"] = int(late_by)
        if reason:
            rec["reason"] = str(reason)[:200]
        if report:
            from core.secret_scrub import scrub_secret_shapes
            rec["report"] = scrub_secret_shapes(str(report))[:LAST_REPORT_MAX_CHARS]
        execute_retry(
            self.db_path,
            "UPDATE cron_jobs SET payload=json_set(payload, '$.last_outcome', json(?)) WHERE id=?",
            (json.dumps(rec), job_id))

    def claim_for_run(self, job_id: str, *, now: Optional[datetime] = None,
                      preempting_only: bool = False) -> Optional[CronJob]:
        """Atomically transition a job 'scheduled' -> 'running'.

        Return the current row from the atomic claim, or None. The due list is
        only a candidate list: another seat may edit a waiting job mid-tick.
        """
        where = "id=? AND enabled=1 AND status='scheduled'"
        params = (job_id,)
        if now is not None:
            where += " AND next_run_at IS NOT NULL AND next_run_at<=?"
            params += (_iso(now),)
        if preempting_only:
            current = self.get(job_id)
            if current is None or not job_preempts(current):
                return None
            # Preserve the Python predicate as the only pre-emption policy;
            # a concurrent payload edit loses this claim and waits one tick.
            where += " AND json(payload)=json(?)"
            params += (json.dumps(current.payload),)
        import time as _time
        # MO-09: the claim names its owner and a lease past the run's own cap.
        lease_base = _time.time() + LEASE_SLACK_SECONDS
        row = execute_retry(
            self.db_path,
            f"UPDATE cron_jobs SET status='running', run_owner=?, "
            f"lease_until=? + MAX(max_duration_seconds, ?) WHERE {where} RETURNING *",
            (run_owner(), lease_base, int(RUN_CAP_CEILING_SECONDS)) + params, fetch="one")
        if row is None:
            return None
        job = self._row_to_job(row)
        job.claim_lease = row["lease_until"]
        return job

    def reclaim_stale_running(self, *, under_lock: bool = True) -> int:
        """Reset jobs stuck in 'running' (process died mid-run) back to 'scheduled'.

        MO-09: a row is reclaimed only when it is provably orphaned — its lease
        ran out or its owner process is gone (:func:`lease_is_live`)
        — one CAS per row on the exact owner read. A live lease is never touched,
        so a worker that stole the tick lock cannot reset (and double-run) a live
        job. A legacy row with no lease is judged by the lock convention: only
        when ``under_lock`` (the scheduler calls this under its TickLock).
        """
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT id, user_id, task, run_owner, lease_until FROM cron_jobs "
                "WHERE status='running' AND enabled=1", (), fetch="all") or []
        except Exception:
            # 125 S16 (B-H11): an unreadable store reclaims nothing, and says so.
            logger.warning("cron: the running jobs could not be read for reclaim",
                           exc_info=True)
            return 0
        n, reclaimed = 0, []
        for r in rows:
            live = lease_is_live(r["run_owner"], r["lease_until"])
            if live or (live is None and not under_lock):
                continue
            rc = execute_retry(
                self.db_path,
                "UPDATE cron_jobs SET status='scheduled', run_owner=NULL, lease_until=NULL, "
                "payload=json_set(payload, '$.cut_run', 1) "
                "WHERE id=? AND status='running' AND enabled=1 "
                "AND run_owner IS ? AND lease_until IS ?",
                (r["id"], r["run_owner"], r["lease_until"])) or 0
            if rc == 1:
                n += 1
                reclaimed.append(r)
        if reclaimed:
            # 056 WS1: name the orphans so each gets a terminal `cron_run
            # cut_by_restart` event — not a bare `started` in the ledger.
            try:
                from types import SimpleNamespace
                for r in reclaimed:
                    emit_cron_run_event(SimpleNamespace(id=r["id"], user_id=r["user_id"], task=r["task"]),
                             "cut_by_restart", "orphaned running row reclaimed")
            except Exception:
                logger.warning("cron: the cut_by_restart event was not written",
                               exc_info=True)
        return n

    def mark_cut_run(self, job_id: str, *, by: str = "restart") -> None:
        """107 WS-3: this job's run was cut (*by* ``restart`` or ``pause``); the next
        tick decides if it may repeat. A crash reclaim stores 1, read as a restart."""
        execute_retry(self.db_path,
                      "UPDATE cron_jobs SET payload=json_set(payload, '$.cut_run', ?) "
                      "WHERE id=? AND status='scheduled'", (str(by), job_id))

    def clear_cut_run(self, job_id: str) -> None:
        execute_retry(self.db_path,
                      "UPDATE cron_jobs SET payload=json_remove(payload, '$.cut_run') "
                      "WHERE id=?", (job_id,))

    def cut_runs(self) -> List[CronJob]:
        """The jobs whose run a crash or a shutdown cut, not yet settled."""
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM cron_jobs WHERE enabled=1 AND status='scheduled' "
            "AND json_extract(payload, '$.cut_run') IS NOT NULL", (), fetch="all") or []
        return [self._row_to_job(r) for r in rows]

    def set_max_duration(self, job_id: str, seconds: int, *,
                         user_id: Optional[str] = None) -> bool:
        """Raise/lower one job's hard cap. Tenant-scoped like ``cancel``; the
        scheduler reads the row fresh on every due tick, so the change applies
        from the next run. Returns False when no row matched."""
        sql = "UPDATE cron_jobs SET max_duration_seconds=? WHERE id=?"
        params: tuple = (int(seconds), job_id)
        if user_id is not None:
            sql += " AND user_id=?"
            params = (int(seconds), job_id, user_id)
        return bool(execute_retry(self.db_path, sql, params))

    def set_schedule(self, job_id: str, schedule_spec: str, *, now: Optional[datetime] = None,
                     user_id: Optional[str] = None) -> bool:
        """056 WS5 (D4): re-time a job from the owner/ops seat and recompute
        ``next_run_at`` from ``now``. An unparseable spec is refused (False), the
        row untouched. Tenant-scoped like ``cancel``."""
        from core.schedule import parse_schedule, ScheduleError
        try:
            schedule = parse_schedule(schedule_spec)
            nxt = schedule.next_run_after(now or datetime.now())
        except ScheduleError:
            return False
        if nxt is None:
            return False
        # A paused job keeps no next run (080 WS-2): the new spec is stored and
        # ``resume`` computes the run from it.
        sql = ("UPDATE cron_jobs SET schedule_spec=?, one_shot=?, next_run_at="
               "CASE WHEN status='paused' THEN NULL ELSE ? END "
               "WHERE id=? AND status IN ('scheduled','running','paused')")
        params: tuple = (schedule_spec, int(schedule.one_shot), nxt.isoformat(), job_id)
        if user_id is not None:
            sql += " AND user_id=?"
            params = params + (user_id,)
        return bool(execute_retry(self.db_path, sql, params))

    def set_priority(self, job_id: str, priority: str, *, user_id: Optional[str] = None) -> bool:
        """056 WS5: set ``payload.priority`` (``money`` | ``ops``) — merge, never
        replace, the payload. Unknown classes are refused."""
        if str(priority) not in (CRON_PRIORITY_MONEY, "ops"):
            return False
        # MO-10: only this key (json_set) — a concurrent `last_outcome` survives.
        sql = "UPDATE cron_jobs SET payload=json_set(payload, '$.priority', ?) WHERE id=?"
        params: tuple = (str(priority), job_id)
        if user_id is not None:
            sql += " AND user_id=?"
            params = params + (user_id,)
        return bool(execute_retry(self.db_path, sql, params))

    def set_task(self, job_id: str, task: str, *, user_id: Optional[str] = None) -> bool:
        """Replace the job's task PROSE — nothing else (schedule, next run, cap,
        payload all untouched). Empty text is refused: a job with no task is
        not an edit, it is a cancel wearing an edit's name."""
        text = (task or "").strip()
        if not text:
            return False
        job = self.get(job_id)
        if job is None or (user_id is not None and job.user_id != user_id):
            return False
        sql = "UPDATE cron_jobs SET task=? WHERE id=?"
        params: tuple = (text, job_id)
        if user_id is not None:
            sql += " AND user_id=?"
            params = params + (user_id,)
        return bool(execute_retry(self.db_path, sql, params))

    def set_preempts(self, job_id: str, value: bool, *,
                     user_id: Optional[str] = None) -> bool:
        """057 WS-C (B7): set ``payload.preempts`` — merge, never replace, the
        payload. Tenant-scoped like ``cancel``."""
        # MO-10: only this key (json_set) — a concurrent `paused` note survives.
        sql = "UPDATE cron_jobs SET payload=json_set(payload, '$.preempts', json(?)) WHERE id=?"
        params: tuple = ("true" if value else "false", job_id)
        if user_id is not None:
            sql += " AND user_id=?"
            params = params + (user_id,)
        return bool(execute_retry(self.db_path, sql, params))

    def prune_cancelled(self, *, older_than_days: float,
                        user_id: Optional[str] = None,
                        now: Optional[datetime] = None,
                        dry_run: bool = False) -> int:
        """057 WS-C (B12): delete cancelled rows older than a cutoff.

        The cron store was used as a config editor — prod carried ~70 cancelled
        rows on 2026-09-20, several of them near-duplicate rails cancelled and
        recreated the same day. Only ``status='cancelled'`` rows are touched, so
        a live or failed job is never removed. Returns rows deleted.

        ``dry_run=True`` (2026-09-21, interface audit C54) COUNTS the rows the
        same predicate would delete and deletes nothing, so a preview and the
        real prune can never disagree — they are one WHERE clause.
        """
        from datetime import timedelta
        cutoff = (now or datetime.now()) - timedelta(days=float(older_than_days))
        where = "WHERE status='cancelled' AND created_at < ?"
        params: tuple = (cutoff.isoformat(),)
        if user_id is not None:
            where += " AND user_id=?"
            params = params + (user_id,)
        if dry_run:
            row = execute_retry(self.db_path,
                                f"SELECT COUNT(*) AS n FROM cron_jobs {where}",
                                params, fetch="one")
            return int((row["n"] if row is not None else 0) or 0)
        n = int(execute_retry(self.db_path, f"DELETE FROM cron_jobs {where}", params) or 0)
        if n:
            self._drop_side_rows(None)
        return n

    def cancel(self, job_id: str, *, user_id: Optional[str] = None,
               note: Optional[Dict[str, Any]] = None) -> bool:
        """Stop the job for good. *note* (who stopped it, via which seat, when)
        lands on ``payload.stopped`` so the row itself answers "who stopped it"."""
        # MO-09: a CAS from every live state; a cancelled row never moves again
        # (re-cancelling it is a no-op that still answers True).
        from core.state.sql import cas_transition
        extra, extra_params = ("user_id=?", (user_id,)) if user_id is not None else ("", ())
        ok = cas_transition(
            self.db_path, "cron_jobs", key="id=?", key_params=(job_id,),
            machine=CRON_JOB_MACHINE, to="cancelled",
            set_cols={"enabled": 0, "run_owner": None, "lease_until": None},
            json_set={"stopped": note} if note else None,
            where_extra=extra, where_params=extra_params)
        if not ok:
            row = self.get(job_id, user_id=user_id)
            return row is not None and row.status == "cancelled"
        self._drop_side_rows(job_id)
        return True
