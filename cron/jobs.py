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
    max_duration_seconds: int = 180
    payload: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    status: str = "scheduled"  # scheduled|running|done|failed|cancelled
    last_run_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


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
                max_duration_seconds INTEGER NOT NULL DEFAULT 180,
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
        return execute_retry(
            self.db_path,
            "UPDATE cron_jobs SET status='scheduled' WHERE status='running' AND enabled=1",
            (),
        ) or 0

    def cancel(self, job_id: str, *, user_id: Optional[str] = None) -> bool:
        sql = "UPDATE cron_jobs SET enabled=0, status='cancelled' WHERE id=?"
        params: tuple = (job_id,)
        if user_id is not None:
            sql += " AND user_id=?"
            params = (job_id, user_id)
        rowcount = execute_retry(self.db_path, sql, params)
        return bool(rowcount)
