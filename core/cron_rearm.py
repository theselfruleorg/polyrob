"""Pull a scheduled cron job to "run on the next tick" — the ONE statement.

Lives in core (tier 0) because two tiers need it and the layering only points
down: ``cron/jobs.py::CronJobStore.run_now`` (the store's own verb) and
``tools/controller/approval_queue.py`` (an owner approval of an ask a cron run
raised re-arms THAT job, because the grant can only be redeemed by a genuine
cron turn — a self-wake into the finished run is a forged turn the money guard
refuses). The schema is the cron store's; this module only knows the one
UPDATE and never creates the database.
"""
from datetime import datetime
from typing import Optional

from core.sqlite_util import execute_retry


def rearm_job(cron_db_path: str, job_id: str, now: datetime) -> bool:
    """Set an enabled, 'scheduled' job's ``next_run_at`` to *now*. A CAS on
    status so a 'running'/'done'/'cancelled' row is never touched. True iff
    the row moved."""
    rowcount = execute_retry(
        cron_db_path,
        "UPDATE cron_jobs SET next_run_at=? WHERE id=? AND enabled=1 "
        "AND status='scheduled'",
        (now.isoformat(), job_id),
    )
    return bool(rowcount)


def cron_db_beside(sibling_db_path: Optional[str]) -> Optional[str]:
    """``cron.db`` in the same data home as *sibling_db_path* (``goals.db``,
    ``wakes.db`` — the autonomy runtime opens them all from ``data_dir``), or
    None when the sibling path is unknown or the file does not exist."""
    import os
    if not sibling_db_path:
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(sibling_db_path)), "cron.db")
    return path if os.path.exists(path) else None
