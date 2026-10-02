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

from core.event_kinds import CRON_CANCELLED, CRON_EDITED, CRON_SCHEDULED
from cron.jobs import CronJob, CronJobStore, default_max_duration_sec
from cron.schedule import ScheduleError, parse_schedule

logger = logging.getLogger("cron.service")

#: 10-minute hard cap for cron sessions, enforced by ``cron/scheduler.py``'s
#: ``asyncio.wait_for``.
#:
#: Jobs may opt into a shorter cap. If one expires while an owner approval opened
#: by that run is still pending, the scheduler records the attempt as deferred
#: rather than failed (see ``cron/approval_defer.py``). A timeout with no such ask
#: still fails.
#: 057 WS-C (B12): kept as the documented fallback constant; the live default is
#: ``cron.jobs.default_max_duration_sec()`` (env CRON_DEFAULT_MAX_DURATION_SEC).
_DEFAULT_MAX_DURATION_S = 600

#: M07 (security analysis 2026-09-23): the shortest recurrence ANY seat may
#: schedule. A `1s` job re-ran on every scheduler tick, each a paid agent run
#: that outlives the session and budget that created it. Module constant, not a
#: flag: five minutes is already 288 paid runs a day.
MIN_INTERVAL_SECONDS = 300
#: A DETERMINISTIC job (``payload.read_verb`` / ``payload.write_verb``) runs no model
#: turn, so the paid-run floor above does not apply to it; one minute is the
#: ticker's own cadence (a collection revealer runs every minute in the mint window).
DETERMINISTIC_MIN_INTERVAL_SECONDS = 60


def _current_gap(job, now) -> float:
    """The job's stored schedule gap in seconds (inf for a one-shot or an unparseable spec,
    which the floor check then leaves alone)."""
    try:
        gap = min_recurrence_seconds(parse_schedule(job.schedule_spec), now)
    except Exception:
        return float("inf")
    return float("inf") if gap is None else gap


def _interval_floor(payload: Optional[Dict[str, Any]]) -> int:
    p = payload or {}
    return (DETERMINISTIC_MIN_INTERVAL_SECONDS if (p.get("read_verb") or p.get("write_verb"))
            else MIN_INTERVAL_SECONDS)


#: M07: a backstop on active (enabled, not finished) jobs per tenant for every
#: seat. Prod ran ~30 for the owner (2026-09-20); this only stops a runaway.
MAX_ACTIVE_JOBS_PER_TENANT = 100

#: M07: the tighter cap on active jobs the AGENT scheduled itself
#: (``payload.authored_by == "agent"``); passed by ``tools/cronjob_tools.py``.
AGENT_MAX_ACTIVE_JOBS = 10

_ACTIVE_STATUSES = ("scheduled", "running")


def _checked_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """W9: normalize ``pause_windows`` and validate ``read_verb`` before any
    write, so a job that cannot run as written is refused, never stored."""
    from cron.read_job import ReadJobError, normalize_pause_windows, validate_read_verb
    from cron.write_job import WriteJobError, validate_write_verb
    try:
        if payload.get("pause_windows") is not None:
            payload["pause_windows"] = normalize_pause_windows(payload["pause_windows"])
        if payload.get("read_verb") is not None:
            payload["read_verb"] = validate_read_verb(payload["read_verb"])
        # Impl handoff E: the one allowlisted write verb (cron/write_job.py).
        if payload.get("write_verb") is not None:
            payload["write_verb"] = validate_write_verb(payload["write_verb"])
    except (ReadJobError, WriteJobError) as exc:
        raise ScheduleError(str(exc)) from exc
    if payload.get("read_verb") and payload.get("write_verb"):
        raise ScheduleError("a job runs either a read_verb or a write_verb, not both")
    return payload


def min_recurrence_seconds(schedule, now: datetime) -> Optional[float]:
    """The shortest gap between two consecutive fires, or None for a one-shot.

    Cron specs are measured over the next few fires rather than reasoned about
    field-by-field: ``*/2 * * * *`` and ``0,1 * * * *`` both resolve honestly.
    """
    if schedule.one_shot:
        return None
    if schedule.kind == "interval":
        return float(schedule.interval_seconds)
    gaps = []
    prev = schedule.next_run_after(now)
    for _ in range(24):
        if prev is None:
            break
        nxt = schedule.next_run_after(prev)
        if nxt is None:
            break
        gaps.append((nxt - prev).total_seconds())
        prev = nxt
    return min(gaps) if gaps else None


class CronService:
    def __init__(self, store: CronJobStore, *, now: Optional[Callable[[], datetime]] = None,
                 id_factory: Optional[Callable[[], str]] = None):
        self.store = store
        self._now = now or datetime.now
        self._id = id_factory or (lambda: uuid.uuid4().hex[:12])

    def schedule(self, *, task: str, schedule_spec: str, user_id: str,
                 payload: Optional[Dict[str, Any]] = None,
                 max_duration_seconds: Optional[int] = None,
                 job_id: Optional[str] = None, via: str = "",
                 max_agent_jobs: Optional[int] = None) -> CronJob:
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
        payload = _checked_payload(dict(payload or {}))  # W9: read_verb, pause_windows
        now = self._now()
        next_run = schedule.next_run_after(now)
        if next_run is None:
            raise ScheduleError(f"schedule {schedule_spec!r} has no future run time")
        # M07: minimum recurrence + per-tenant active-job caps. ``max_agent_jobs``
        # (the agent tool passes AGENT_MAX_ACTIVE_JOBS) counts only jobs the
        # agent scheduled itself, so an owner's own jobs never starve it.
        gap = min_recurrence_seconds(schedule, now)
        floor = _interval_floor(payload)
        if gap is not None and gap < floor:
            raise ScheduleError(
                f"schedule {schedule_spec!r} recurs every {int(gap)}s; the minimum "
                f"is {floor // 60} minute(s)")
        active = [j for j in self.store.list(user_id=user_id, enabled_only=True)
                  if j.status in _ACTIVE_STATUSES]
        if len(active) >= MAX_ACTIVE_JOBS_PER_TENANT:
            raise ScheduleError(
                f"this tenant already has {len(active)} active cron jobs (the cap is "
                f"{MAX_ACTIVE_JOBS_PER_TENANT}); cancel one first")
        if max_agent_jobs is not None:
            mine = [j for j in active
                    if str((j.payload or {}).get("authored_by") or "") == "agent"]
            if len(mine) >= max_agent_jobs:
                raise ScheduleError(
                    f"you already have {len(mine)} active self-scheduled cron jobs "
                    f"(the cap is {max_agent_jobs}); cancel one first, or ask the "
                    f"owner to schedule this")
        job = CronJob(
            id=job_id or self._id(), task=task, schedule_spec=schedule_spec,
            user_id=user_id, next_run_at=next_run, one_shot=schedule.one_shot,
            max_duration_seconds=(int(max_duration_seconds)
                                  if max_duration_seconds is not None
                                  else default_max_duration_sec()),
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

    def edit(self, job_id: str, *, user_id: str, via: str = "",
             old_text: Optional[str] = None, new_text: Optional[str] = None,
             schedule_spec: Optional[str] = None,
             max_duration_seconds: Optional[int] = None,
             payload_updates: Optional[Dict[str, Any]] = None) -> List[str]:
        """Amend a live job IN PLACE and return the names of the changed fields.

        Why: with only schedule/list/cancel, changing one number in a rail meant
        cancel + re-schedule — a full rewrite of the task prose from memory. The
        0.05 buyback rewrite (2026-09-24) silently dropped its reconcile gate and
        row-write ordering that way. The task is therefore PATCHED, never
        replaced: ``old_text`` must occur exactly once and is swapped for
        ``new_text``; everything else in the prose survives byte-for-byte.

        Validates EVERY requested change before writing any (raises
        ScheduleError with the reason); ``payload_updates`` merges one key at a
        time (a value of None drops the key). Records one ``CRON_EDITED`` event.
        """
        job = self.store.get(job_id, user_id=user_id)
        if job is None:
            raise ScheduleError(f"no cron job {job_id!r} for this tenant")
        if job.status not in _ACTIVE_STATUSES:
            raise ScheduleError(f"cron job {job_id!r} is {job.status}; only a live job can be edited")
        new_task = None
        if old_text is not None or new_text is not None:
            if not old_text:
                raise ScheduleError("a task edit needs old_text (the exact passage to replace)")
            count = job.task.count(old_text)
            if count != 1:
                raise ScheduleError(
                    f"old_text occurs {count} times in the task; it must match exactly once "
                    f"(copy it from cronjob_show, and include more context if it repeats)")
            new_task = job.task.replace(old_text, new_text or "", 1)
            if not new_task.strip():
                raise ScheduleError("the edit would leave an empty task; cancel the job instead")
        if schedule_spec is not None:
            schedule = parse_schedule(schedule_spec)  # raises ScheduleError
            now = self._now()
            if schedule.next_run_after(now) is None:
                raise ScheduleError(f"schedule {schedule_spec!r} has no future run time")
            merged = {**(job.payload or {}), **(payload_updates or {})}
            gap = min_recurrence_seconds(schedule, now)
            floor = _interval_floor({k: v for k, v in merged.items() if v is not None})
            if gap is not None and gap < floor:
                raise ScheduleError(
                    f"schedule {schedule_spec!r} recurs every {int(gap)}s; the minimum "
                    f"is {floor // 60} minute(s)")
        if payload_updates:
            payload_updates = dict(payload_updates)
            checked = _checked_payload({k: v for k, v in payload_updates.items()
                                        if k in ("read_verb", "write_verb", "pause_windows")
                                        and v is not None})
            payload_updates.update(checked)
            after = {**(job.payload or {}), **payload_updates}
            if after.get("read_verb") and after.get("write_verb"):
                raise ScheduleError("a job runs either a read_verb or a write_verb, not both")
            if schedule_spec is None and not _interval_floor(
                    {k: v for k, v in after.items() if v is not None}) <= _current_gap(job, self._now()):
                raise ScheduleError(
                    f"the job recurs faster than {MIN_INTERVAL_SECONDS // 60} minutes; only a "
                    f"read or write job may — change the schedule in the same edit")
        target = (payload_updates or {}).get("deliver")
        if target is not None:
            from cron.delivery import allowed_targets
            if target not in allowed_targets():
                raise ScheduleError(
                    f"unknown delivery target {target!r}; valid: "
                    f"{', '.join(allowed_targets())}, or 'none'")
        changed: List[str] = []
        if new_task is not None and self.store.set_task(job_id, new_task, user_id=user_id):
            changed.append("task")
        if schedule_spec is not None and self.store.set_schedule(job_id, schedule_spec,
                                                                 now=self._now(), user_id=user_id):
            changed.append("schedule")
        if max_duration_seconds is not None and self.store.set_max_duration(
                job_id, int(max_duration_seconds), user_id=user_id):
            changed.append("max_duration_seconds")
        if payload_updates:
            from cron.rig_edit import set_job_payload_key
            for key, value in payload_updates.items():
                if set_job_payload_key(self.store, job_id, key, value, user_id=user_id):
                    changed.append(key)
        if changed:
            extra: Dict[str, Any] = {"fields": changed}
            if "task" in changed:
                extra["old_text"] = (old_text or "")[:500]
                extra["new_text"] = (new_text or "")[:500]
            if "schedule" in changed:
                extra["schedule_spec"] = schedule_spec
            self._audit(CRON_EDITED, user_id=user_id, via=via, job_id=job_id, **extra)
        return changed

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
