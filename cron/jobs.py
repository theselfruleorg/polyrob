"""SQLite-backed cron job store (roadmap P5).

A single ``cron_jobs`` table holding durable schedule state. Uses WAL mode with a
jittered retry on write contention (mirrors Reference ``SessionDB`` and foreshadows
the P6 session-registry migration). The store is intentionally schedule-agnostic:
callers compute ``next_run_at`` with :mod:`cron.schedule` and hand it in.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.sqlite_util import execute_retry


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
    status: str = "scheduled"  # scheduled|running|done|failed|cancelled
    last_run_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


#: 056 WS5: the priority class a job declares in ``payload.priority``. ``money``
#: marks the treasury rails (EXIT / SAFETY / WATCHER / SCOUT / BUYBACK); a due
#: money job pre-empts a running board goal (GOAL_YIELD_FOR_MONEY_RAIL).
CRON_PRIORITY_MONEY = "money"


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


def is_money_job(job) -> bool:
    try:
        return str((getattr(job, "payload", None) or {}).get("priority") or "").lower() == CRON_PRIORITY_MONEY
    except Exception:
        return False


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
        execute_retry(
            self.db_path,
            """
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
        )

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
                         next_run_at: Optional[datetime], status: str) -> None:
        execute_retry(
            self.db_path,
            "UPDATE cron_jobs SET last_run_at=?, next_run_at=?, status=? WHERE id=?",
            (_iso(last_run_at), _iso(next_run_at), status, job_id),
        )

    def run_now(self, job_id: str, now: datetime) -> bool:
        """Pull an enabled, 'scheduled' job's ``next_run_at`` to *now* so the
        next tick runs it. A CAS on status so a 'running'/'done'/'cancelled'
        row is never touched; returns True iff the row moved. Used by the owner
        queue (2026-09-18): approving an ask a cron run raised re-arms the job,
        because the grant can only be redeemed by a genuine cron turn."""
        from core.cron_rearm import rearm_job
        return rearm_job(self.db_path, job_id, now)

    def set_status(self, job_id: str, status: str) -> None:
        execute_retry(self.db_path, "UPDATE cron_jobs SET status=? WHERE id=?", (status, job_id))

    def claim_for_run(self, job_id: str) -> bool:
        """Atomically transition a job 'scheduled' -> 'running'.

        Returns True iff THIS caller won the claim (rowcount == 1). A compare-and-set
        on status='scheduled' so two ticks can never both run the same job — the
        non-CAS set_status('running') it replaces could double-run a job if a tick
        ever raced a reclaim.
        """
        rowcount = execute_retry(
            self.db_path,
            "UPDATE cron_jobs SET status='running' WHERE id=? AND status='scheduled'",
            (job_id,),
        )
        return bool(rowcount)

    def reclaim_stale_running(self) -> int:
        """Reset jobs stuck in 'running' (process died mid-run) back to 'scheduled'.

        Returns the number of rows reclaimed. MUST be called only under the held cron
        TickLock (the scheduler does this at the start of each tick). Ticks are
        serialized by the lock and always write a terminal/rescheduled status after a
        run, so any 'running' row observed under the lock is genuinely orphaned by a
        crash and safe to reclaim. Calling it WITHOUT the lock (e.g. from __init__)
        could reset a live job and cause a double-run.
        """
        # 056 WS1: name the orphans so each gets a terminal `cron_run cut_by_restart`
        # event — before this a restart mid-run left the ledger with a bare `started`.
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT id, user_id, task FROM cron_jobs WHERE status='running' AND enabled=1",
                (), fetch="all") or []
        except Exception:
            rows = []
        n = execute_retry(
            self.db_path,
            "UPDATE cron_jobs SET status='scheduled' WHERE status='running' AND enabled=1",
            (),
        ) or 0
        if n and rows:
            try:
                from types import SimpleNamespace
                from cron.runner import _cron_ev
                for r in rows:
                    _cron_ev(SimpleNamespace(id=r["id"], user_id=r["user_id"], task=r["task"]),
                             "cut_by_restart", "orphaned running row reclaimed")
            except Exception:
                pass
        return n

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
        from cron.schedule import parse_schedule, ScheduleError
        try:
            nxt = parse_schedule(schedule_spec).next_run_after(now or datetime.now())
        except ScheduleError:
            return False
        if nxt is None:
            return False
        sql = "UPDATE cron_jobs SET schedule_spec=?, next_run_at=? WHERE id=? AND status IN ('scheduled','running')"
        params: tuple = (schedule_spec, nxt.isoformat(), job_id)
        if user_id is not None:
            sql += " AND user_id=?"
            params = params + (user_id,)
        return bool(execute_retry(self.db_path, sql, params))

    def set_priority(self, job_id: str, priority: str, *, user_id: Optional[str] = None) -> bool:
        """056 WS5: set ``payload.priority`` (``money`` | ``ops``) — merge, never
        replace, the payload. Unknown classes are refused."""
        if str(priority) not in (CRON_PRIORITY_MONEY, "ops"):
            return False
        job = self.get(job_id)
        if job is None or (user_id is not None and job.user_id != user_id):
            return False
        payload = dict(job.payload or {})
        payload["priority"] = str(priority)
        sql = "UPDATE cron_jobs SET payload=? WHERE id=?"
        params: tuple = (json.dumps(payload), job_id)
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
        job = self.get(job_id)
        if job is None or (user_id is not None and job.user_id != user_id):
            return False
        payload = dict(job.payload or {})
        payload["preempts"] = bool(value)
        sql = "UPDATE cron_jobs SET payload=? WHERE id=?"
        params: tuple = (json.dumps(payload), job_id)
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
        return int(execute_retry(self.db_path,
                                 f"DELETE FROM cron_jobs {where}", params) or 0)

    def cancel(self, job_id: str, *, user_id: Optional[str] = None) -> bool:
        sql = "UPDATE cron_jobs SET enabled=0, status='cancelled' WHERE id=?"
        params: tuple = (job_id,)
        if user_id is not None:
            sql += " AND user_id=?"
            params = (job_id, user_id)
        rowcount = execute_retry(self.db_path, sql, params)
        return bool(rowcount)
