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

from core.event_kinds import (CRON_CANCELLED, CRON_EDITED, CRON_PAUSED, CRON_RESUMED,
                              CRON_RUN_NOW, CRON_SCHEDULED)
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

_ACTIVE_STATUSES = ("scheduled", "running")
#: A live job: it has not ended. A paused job is live (it counts toward the caps,
#: can be edited, and is the duplicate a second identical ``/job new`` warns of).
LIVE_STATUSES = ("scheduled", "running", "paused")


# ---------------------------------------------------------------------------
# Owner-facing helpers every seat shares (080 WS-2). One text per fact.
# ---------------------------------------------------------------------------

def scheduler_off_note() -> str:
    """"" when the scheduler runs; else the ONE sentence every seat prints.

    Before 080 three seats named three different remedies for this one state
    (and two of them named a posture member whose direct env line is ignored).
    The remedy is the posture that turns the scheduler on. A probe fault is not
    "on": it says that it could not check.
    """
    try:
        from tools.cronjob_tools import cron_enabled
        if cron_enabled():
            return ""
    except Exception as exc:  # noqa: BLE001 — a fault is reported, never "on"
        logger.warning("scheduler probe failed", exc_info=True)
        return (f"I could not check whether the scheduler is on ({type(exc).__name__}) "
                f"— the job is stored, but I cannot promise that anything will run it.")
    from core.remedy import flag_command
    return ("The scheduler is off — nothing will run this job; turn it on with "
            f"`{flag_command('AUTONOMY_POSTURE', 'full')}` (takes effect: restart).")


def duplicate_job_text(job) -> str:
    """The refusal a second identical ``/job new`` gets, naming the live job."""
    return (f"Job {job.id[:8]} already does this on {job.schedule_spec} ({job.status}) "
            f"— nothing added. Change it with /job edit, or stop it with /job stop.")


def last_outcome(job) -> str:
    """The last run's outcome and time, e.g. "done 10-13 08:00"; "never run"."""
    rec = (getattr(job, "payload", None) or {}).get("last_outcome") or {}
    at = rec.get("at") or ""
    when = at[5:16].replace("T", " ") if at else ""
    if rec.get("outcome"):
        return f"{rec['outcome']} {when}".strip()
    if getattr(job, "last_run_at", None):
        return f"ran {job.last_run_at.strftime('%m-%d %H:%M')}"
    return "never run"


def next_run_text(job) -> str:
    """The next run, or the reason there is none. A paused job shows no time."""
    if job.status == "paused":
        return "paused"
    if job.status in ("cancelled", "done", "failed"):
        return job.status if job.status != "cancelled" else "stopped"
    return job.next_run_at.strftime("%Y-%m-%d %H:%M") if job.next_run_at else "-"


def parse_new_job_args(tokens: List[str]) -> "tuple[str, str]":
    """``<when> <what>`` -> ``(when, what)``. ONE grammar on every seat.

    Accepts ``every day 08:00 | do it``, ``'every day 08:00' do it`` and the
    bare ``every day 08:00 do it`` (the longest leading run of words that
    parses as a schedule is the schedule). Raises ``ScheduleError`` when no
    leading words parse or nothing is left for <what>.
    """
    raw = " ".join(str(t) for t in tokens).strip()
    if "|" in raw:
        when, _, what = raw.partition("|")
        when, what = when.strip().strip("'\""), what.strip()
        if not when or not what:
            raise ScheduleError("both parts are needed: <when> | <what>")
        parse_schedule(when)  # raises ScheduleError with the reason
        return when, what
    import shlex
    try:
        words = shlex.split(raw)
    except ValueError:
        words = raw.split()
    for n in range(min(len(words) - 1, 6), 0, -1):
        when = " ".join(words[:n])
        try:
            parse_schedule(when)
        except ScheduleError:
            continue
        return when, " ".join(words[n:]).strip()
    raise ScheduleError("I could not read a schedule at the start "
                        "(e.g. 30m, 'every monday 09:00', '0 9 * * *')")


def _checked_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """W9: normalize ``pause_windows`` and validate ``read_verb`` before any
    write, so a job that cannot run as written is refused, never stored."""
    from cron.read_job import ReadJobError, normalize_pause_windows, validate_read_verb
    from cron.write_job import WriteJobError, validate_write_verb
    from cron.read_job import PrecheckError, validate_precheck
    try:
        if payload.get("pause_windows") is not None:
            payload["pause_windows"] = normalize_pause_windows(payload["pause_windows"])
        if payload.get("read_verb") is not None:
            payload["read_verb"] = validate_read_verb(payload["read_verb"])
        # Impl handoff E: the one allowlisted write verb (cron/write_job.py).
        if payload.get("write_verb") is not None:
            payload["write_verb"] = validate_write_verb(payload["write_verb"])
        # 2026-10-14 review A-12: a watch job's $0 pre-check (cron/read_job.py).
        if payload.get("precheck") is not None:
            payload["precheck"] = validate_precheck(payload["precheck"])
    except (ReadJobError, WriteJobError, PrecheckError) as exc:
        raise ScheduleError(str(exc)) from exc
    _no_mixed_kinds(payload)
    return payload


def _no_mixed_kinds(payload: Dict[str, Any]) -> None:
    if payload.get("read_verb") and payload.get("write_verb"):
        raise ScheduleError("a job runs either a read_verb or a write_verb, not both")
    if payload.get("precheck") and (payload.get("read_verb") or payload.get("write_verb")):
        raise ScheduleError("a precheck guards a model run; a read_verb or write_verb job "
                            "runs no model, so it cannot carry one")


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
                 owner_seat: bool = False) -> CronJob:
        """Validate the spec, compute the first run, and persist the job.

        0011 — an owner stop is state. *owner_seat* is the CALLER's word that
        this create is the owner's typed act (an owner seat via
        ``core.owner_create`` or the ``polyrob job`` CLI — never the agent's
        tool, even on an owner turn); a payload field is never read for it. A job
        pinning any lane (``core.cron_lanes.gated_lanes``) the owner stopped is
        refused (``core.cron_lanes.LaneStopped``) unless *owner_seat*, and the
        owner's create lifts the stop. While ``/pause cron`` is on the job
        is stored PAUSED and runs after the owner resumes. Both are said on the
        job (``core.cron_lanes.schedule_notes``).

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
        # M07: minimum recurrence + the per-tenant active-job cap.
        gap = min_recurrence_seconds(schedule, now)
        floor = _interval_floor(payload)
        if gap is not None and gap < floor:
            raise ScheduleError(
                f"schedule {schedule_spec!r} recurs every {int(gap)}s; the minimum "
                f"is {floor // 60} minute(s)")
        active = [j for j in self.store.list(user_id=user_id, enabled_only=True)
                  if j.status in LIVE_STATUSES]
        if len(active) >= MAX_ACTIVE_JOBS_PER_TENANT:
            raise ScheduleError(
                f"this tenant already has {len(active)} active cron jobs (the cap is "
                f"{MAX_ACTIVE_JOBS_PER_TENANT}); cancel one first")
        from core import cron_lanes as lanes
        stops = lanes.lane_gate(self.store, user_id=user_id,
                                lanes=lanes.gated_lanes(payload),
                                owner_seat=owner_seat)  # raises LaneStopped
        lanes.check_pinned_skills(payload, user_id=user_id)  # raises UnknownPinnedSkill
        paused = lanes.scope_pause_note(payload, now)
        if paused:
            payload["paused"] = paused
        job = CronJob(
            id=job_id or self._id(), task=task, schedule_spec=schedule_spec,
            user_id=user_id, next_run_at=None if paused else next_run,
            one_shot=schedule.one_shot,
            max_duration_seconds=(int(max_duration_seconds)
                                  if max_duration_seconds is not None
                                  else default_max_duration_sec()),
            payload=payload or {}, created_at=now,
            status="paused" if paused else "scheduled",
        )
        stored = self.store.add(job)
        notes = [lanes.lift_after_owner_create(self.store, user_id=user_id,
                                               lane=lane, stop=stop)
                 for lane, stop in stops]
        if paused:
            notes.append(lanes.scope_pause_text(paused))
        stored.schedule_notes = notes
        self._audit(CRON_SCHEDULED, user_id=user_id, via=via, job_id=stored.id,
                    schedule_spec=schedule_spec, one_shot=stored.one_shot)
        return stored

    def list_jobs(self, user_id: Optional[str] = None) -> List[CronJob]:
        return self.store.list(user_id=user_id)

    def find_duplicate(self, *, user_id: str, task: str,
                       schedule_spec: str) -> Optional[CronJob]:
        """A live job of this tenant with the same task and schedule, or None."""
        want_task, want_spec = (task or "").strip(), (schedule_spec or "").strip()
        for j in self.store.list(user_id=user_id, enabled_only=True):
            if (j.status in LIVE_STATUSES and j.task.strip() == want_task
                    and j.schedule_spec.strip() == want_spec):
                return j
        return None

    @staticmethod
    def _actor(via: str) -> str:
        via = str(via or "").strip()
        if not via:
            # 2026-10-06: a cancel with via="" left "who stopped it?" unanswerable.
            raise ValueError("an owner job operation needs the actor's seat (via=…)")
        return via

    def cancel(self, job_id: str, *, user_id: Optional[str] = None,
               via: str) -> bool:
        """Stop a job for good. *via* (the seat or ``agent``) is REQUIRED and
        lands on the row (``payload.stopped``) and on the ``CRON_CANCELLED``
        event. A no-op cancel (already gone / wrong tenant) records nothing."""
        via = self._actor(via)
        note = {"by": str(user_id or ""), "via": via, "at": self._now().isoformat()}
        ok = self.store.cancel(job_id, user_id=user_id, note=note)
        if ok:
            self._audit(CRON_CANCELLED, user_id=user_id or "", via=via,
                        job_id=job_id)
        return ok

    def pause(self, job_id: str, *, user_id: str, via: str) -> bool:
        """Pause one job: its own state ('paused', no next run). True iff it moved."""
        via = self._actor(via)
        note = {"by": str(user_id or ""), "via": via, "at": self._now().isoformat()}
        ok = self.store.pause(job_id, user_id=user_id, note=note)
        if ok:
            self._audit(CRON_PAUSED, user_id=user_id, via=via, job_id=job_id)
        return ok

    def resume(self, job_id: str, *, user_id: str, via: str) -> bool:
        """Resume a paused job at its next slot (a one-shot whose time passed
        while paused runs on the next tick). True iff it moved."""
        via = self._actor(via)
        job = self.store.get(job_id, user_id=user_id)
        if job is None or job.status != "paused":
            return False
        now = self._now()
        try:
            nxt = parse_schedule(job.schedule_spec).next_run_after(now)
        except ScheduleError:
            nxt = None
        ok = self.store.resume(job_id, user_id=user_id, next_run_at=nxt or now)
        if ok:
            self._audit(CRON_RESUMED, user_id=user_id, via=via, job_id=job_id)
        return ok

    def run_now(self, job_id: str, *, user_id: str, via: str) -> str:
        """Pull a scheduled job to the next tick. Returns ``""`` on success, or
        the reason it did not move ("paused", "running", "stopped", "missing")."""
        via = self._actor(via)
        job = self.store.get(job_id, user_id=user_id)
        if job is None:
            return "missing"
        if job.status != "scheduled" or not job.enabled:
            return {"cancelled": "stopped"}.get(job.status, job.status)
        if not self.store.run_now(job_id, self._now()):
            return job.status
        self._audit(CRON_RUN_NOW, user_id=user_id, via=via, job_id=job_id)
        return ""

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
        if job.status not in LIVE_STATUSES:
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
                                        if k in ("read_verb", "write_verb", "pause_windows",
                                                 "precheck")
                                        and v is not None})
            payload_updates.update(checked)
            after = {**(job.payload or {}), **payload_updates}
            _no_mixed_kinds({k: v for k, v in after.items() if v is not None})
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
