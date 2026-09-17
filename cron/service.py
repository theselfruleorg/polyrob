"""CronService — the schedule/list/cancel API over the job store (roadmap P5).

Thin, fully testable orchestration of :mod:`cron.schedule` + :mod:`cron.jobs`.
The agent-facing ``cronjob`` tool (``tools/cronjob_tools.py``) and any HTTP/CLI
surface delegate here so scheduling logic lives in exactly one place.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core.event_kinds import CRON_CANCELLED, CRON_SCHEDULED
from cron.jobs import CronJob, CronJobStore
from cron.schedule import ScheduleError, parse_schedule

logger = logging.getLogger("cron.service")

#: 10-minute hard cap for cron sessions, enforced by ``cron/scheduler.py``'s
#: ``asyncio.wait_for``.
#:
#: Jobs may opt into a shorter cap. If one expires while an owner approval opened
#: by that run is still pending, the scheduler records the attempt as deferred
#: rather than failed (see ``cron/approval_defer.py``). A timeout with no such ask
#: still fails.
_DEFAULT_MAX_DURATION_S = 600


class CronService:
    def __init__(self, store: CronJobStore, *, now: Optional[Callable[[], datetime]] = None,
                 id_factory: Optional[Callable[[], str]] = None):
        self.store = store
        self._now = now or datetime.now
        self._id = id_factory or (lambda: uuid.uuid4().hex[:12])

    def schedule(self, *, task: str, schedule_spec: str, user_id: str,
                 payload: Optional[Dict[str, Any]] = None,
                 max_duration_seconds: int = _DEFAULT_MAX_DURATION_S,
                 job_id: Optional[str] = None, via: str = "") -> CronJob:
        """Validate the spec, compute the first run, and persist the job.

        Raises ScheduleError for an unparseable spec or one with no future run
        (e.g. a one-shot timestamp already in the past).

        A29: a successful schedule records a ``CRON_SCHEDULED`` audit event with
        the actor (``user_id``) and the caller's ``via`` surface label — so the
        console, CLI and Telegram paths are all audited here, at the one service
        every seat calls. Fail-open (telemetry never breaks the schedule).

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
        stored = self.store.add(job)
        self._audit(CRON_SCHEDULED, user_id=user_id, via=via, job_id=stored.id,
                    schedule_spec=schedule_spec, one_shot=stored.one_shot)
        return stored

    def list_jobs(self, user_id: Optional[str] = None) -> List[CronJob]:
        return self.store.list(user_id=user_id)

    def cancel(self, job_id: str, *, user_id: Optional[str] = None,
               via: str = "") -> bool:
        """Cancel a job. A29: a successful cancel records a ``CRON_CANCELLED``
        audit event (actor + ``via``). A no-op cancel (already gone / wrong
        tenant) records nothing — only a real state change is a domain event."""
        ok = self.store.cancel(job_id, user_id=user_id)
        if ok:
            self._audit(CRON_CANCELLED, user_id=user_id or "", via=via,
                        job_id=job_id)
        return ok

    def _audit(self, kind: str, *, user_id: str, via: str, job_id: str,
               **extra: Any) -> None:
        """Record one cron domain event to the durable audit log. Fail-open.

        This is the SERVICE-level event — distinct from the console's
        ``console_write(CONSOLE_CRON_CANCEL)`` (the console-action audit). Since
        every surface schedules/cancels through this ONE service, recording here
        audits the console, CLI and Telegram paths alike; ``via`` names the
        surface when the caller supplies it. Respects ``event_log_enabled()`` —
        no new flag. NEVER raises (telemetry must not break the write it
        observes), matching ``cron/runner.py::_cron_ev``.
        """
        try:
            from core.event_log import event_log_enabled, get_event_log
            if not event_log_enabled():
                return
            get_event_log().record(
                kind, user_id=str(user_id or ""), source="cron",
                attrs={"via": via or "", "job_id": job_id, **extra})
        except Exception as exc:  # telemetry must never break schedule/cancel
            logger.debug("cron audit (%s) failed: %s", kind, exc)
