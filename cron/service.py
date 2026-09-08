"""CronService — the schedule/list/cancel API over the job store (roadmap P5).

Thin, fully testable orchestration of :mod:`cron.schedule` + :mod:`cron.jobs`.
The agent-facing ``cronjob`` tool (``tools/cronjob_tools.py``) and any HTTP/CLI
surface delegate here so scheduling logic lives in exactly one place.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from cron.jobs import CronJob, CronJobStore
from cron.schedule import ScheduleError, parse_schedule

#: 3-minute hard cap for cron sessions, enforced by ``cron/scheduler.py``'s
#: ``asyncio.wait_for``.
#:
#: ⚠️ This is SHORTER than the durable owner-approval wait
#: (``tools/controller/approval.py::approval_wait_timeout_sec`` -> 300s for the
#: ``owner_queue`` provider), so an approval-gated verb attempted inside a cron
#: run is always cut off before the owner's window elapses. Raising the cap to
#: cover it is the wrong lever — it would let EVERY cron run occupy the tick for
#: 5+ minutes. The scheduler resolves it in the accounting instead: a cap timeout
#: attributable to an OPEN owner ask THIS run raised is recorded as *deferred*,
#: not as the job's failure, and the durable ask stays redeemable via ``/approve``
#: (see ``cron/approval_defer.py``). A timeout with no such ask still fails.
_DEFAULT_MAX_DURATION_S = 180


class CronService:
    def __init__(self, store: CronJobStore, *, now: Optional[Callable[[], datetime]] = None,
                 id_factory: Optional[Callable[[], str]] = None):
        self.store = store
        self._now = now or datetime.now
        self._id = id_factory or (lambda: uuid.uuid4().hex[:12])

    def schedule(self, *, task: str, schedule_spec: str, user_id: str,
                 payload: Optional[Dict[str, Any]] = None,
                 max_duration_seconds: int = _DEFAULT_MAX_DURATION_S,
                 job_id: Optional[str] = None) -> CronJob:
        """Validate the spec, compute the first run, and persist the job.

        Raises ScheduleError for an unparseable spec or one with no future run
        (e.g. a one-shot timestamp already in the past).

        NOTE (ME-D2): ``skip_memory`` was removed from this signature — it was
        never consumed by the runner or the goal dispatcher (dead, inert knob).
        A cron run gets cross-session memory recall like any other session; see
        ``CronJob.skip_memory`` in ``cron/jobs.py`` for the dormant DB column.
        """
        schedule = parse_schedule(schedule_spec)  # raises ScheduleError
        now = self._now()
        next_run = schedule.next_run_after(now)
        if next_run is None:
            raise ScheduleError(f"schedule {schedule_spec!r} has no future run time")
        job = CronJob(
            id=job_id or self._id(), task=task, schedule_spec=schedule_spec,
            user_id=user_id, next_run_at=next_run, one_shot=schedule.one_shot,
            max_duration_seconds=max_duration_seconds,
            payload=payload or {}, created_at=now,
        )
        return self.store.add(job)

    def list_jobs(self, user_id: Optional[str] = None) -> List[CronJob]:
        return self.store.list(user_id=user_id)

    def cancel(self, job_id: str, *, user_id: Optional[str] = None) -> bool:
        return self.store.cancel(job_id, user_id=user_id)
