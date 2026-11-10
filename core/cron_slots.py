"""Job slots: which minutes a model run holds, and the nearest free minute.

The tick runs due jobs one at a time, so two model runs in one slot do not run
together: the later one starts only after the earlier one ends, up to its cap. The
agent saw a note about this and still put jobs in one slot, so the owner had to
ask for the correction. ``cronjob_schedule`` and ``cronjob_edit`` now place a job
themselves (:func:`place`), unless the job keeps an exact time.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, List, Optional

from core.schedule import Schedule, ScheduleError, parse_schedule, shift_spec

#: Payload keys of a job that runs no model turn.
MODEL_FREE_KEYS = ("digest", "read_verb", "write_verb", "script")
#: The time one model run is taken to hold. An estimate: a run can hold more.
SLOT_MINUTES = 10
#: A job that recurs faster than this fills the clock, so no minute can avoid it.
MIN_SLOTTED_GAP = timedelta(minutes=30)
HORIZON = timedelta(days=7)
SHIFT_STEP_MINUTES = 5
MAX_SHIFT_MINUTES = 55
#: The payload key of a job that keeps its time. Every owner seat stamps it.
EXACT_KEY = "exact_time"

_HOLDER_STATUSES = ("scheduled", "running")


def server_clock() -> str:
    """The zone of the server clock that schedules read: ``UTC`` or ``UTC+7:00``."""
    offset = datetime.now().astimezone().utcoffset()
    if not offset:
        return "UTC"
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60}:{abs(minutes) % 60:02d}"


def holds_a_slot(payload: Optional[dict]) -> bool:
    """A run with no model turn ends in seconds, so it holds no slot."""
    p = payload or {}
    return not (any(p.get(k) for k in MODEL_FREE_KEYS) or p.get("wake_agent", True) is False)


def _fires(schedule: Schedule, start: datetime, *, first: Optional[datetime] = None
           ) -> List[datetime]:
    """Fire times inside the horizon of *start* (*first* is a stored next run)."""
    end = start + HORIZON
    out = [first] if first is not None and first <= end else []
    at = first or start
    while True:
        at = schedule.next_run_after(at)
        if at is None or at > end:
            return out
        out.append(at)


def _too_frequent(fires: List[datetime]) -> bool:
    return any(b - a < MIN_SLOTTED_GAP for a, b in zip(fires, fires[1:]))


@dataclass(frozen=True)
class Overlap:
    job: Any
    at: datetime


class SlotTable:
    """The fire times of a tenant's live model-run jobs, inside the horizon."""

    def __init__(self, jobs: Iterable[Any], now: datetime):
        self.now = now
        rows = []
        for job in jobs:
            if job.status not in _HOLDER_STATUSES or not holds_a_slot(job.payload):
                continue
            fires = self.job_fires(job)
            if fires and not _too_frequent(fires):
                rows.extend((at, job) for at in fires)
        rows.sort(key=lambda r: r[0])
        self._times = [r[0] for r in rows]
        self._jobs = [r[1] for r in rows]

    def job_fires(self, job: Any) -> List[datetime]:
        try:
            return _fires(parse_schedule(job.schedule_spec), self.now, first=job.next_run_at)
        except ScheduleError:
            return []

    def overlap(self, fires: Iterable[datetime], *, exclude_id: Optional[str] = None
                ) -> Optional[Overlap]:
        """The first other job within one slot of any of *fires*, or None."""
        window = timedelta(minutes=SLOT_MINUTES)
        for at in fires:
            low = bisect.bisect_right(self._times, at - window)
            high = bisect.bisect_left(self._times, at + window)
            for job in self._jobs[low:high]:
                if job.id != exclude_id:
                    return Overlap(job, at)
        return None

    def free_spec(self, spec: str, *, exclude_id: Optional[str] = None) -> Optional[str]:
        """The nearest later form of *spec* that overlaps nothing, or None."""
        for minutes in range(SHIFT_STEP_MINUTES, MAX_SHIFT_MINUTES + 1, SHIFT_STEP_MINUTES):
            moved = shift_spec(spec, minutes)
            if moved is None:
                return None
            try:
                fires = _fires(parse_schedule(moved), self.now)
            except ScheduleError:
                return None
            if self.overlap(fires, exclude_id=exclude_id) is None:
                return moved
        return None


@dataclass(frozen=True)
class Placement:
    spec: str
    #: The job the asked time overlapped, or None when that time was free.
    overlap: Optional[Overlap] = None
    #: True when *spec* is not the asked spec.
    moved: bool = False


def place(spec: str, payload: Optional[dict], jobs: Iterable[Any], now: datetime,
          *, exclude_id: Optional[str] = None) -> Placement:
    """Where a job asked for at *spec* goes. An unparseable spec comes back
    unchanged: the service refuses it, with the reason."""
    try:
        schedule = parse_schedule(spec)
    except ScheduleError:
        return Placement(spec)
    if not holds_a_slot(payload):
        return Placement(spec)
    table = SlotTable(jobs, now)
    fires = _fires(schedule, now)
    if _too_frequent(fires):
        return Placement(spec)
    hit = table.overlap(fires, exclude_id=exclude_id)
    if hit is None:
        return Placement(spec)
    moved = table.free_spec(spec, exclude_id=exclude_id)
    return Placement(moved or spec, overlap=hit, moved=bool(moved))
