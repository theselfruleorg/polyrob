"""Schedule parsing + next-run computation for the cron subsystem (roadmap P5).

Pure and tz-naive (local wall-clock) so it is deterministic and unit-testable —
every entry point takes the reference time explicitly. Supported spec formats:

- duration interval ............ ``30m``, ``2h``, ``1d``, ``45s`` (also ``every 15m``)
- one-shot ISO timestamp ....... ``2026-06-07T09:30:00``
- 5-field cron ................. ``*/15 * * * *`` (min hour dom mon dow; dow 0/7=Sun)
- every-phrase ................. ``every day 14:30``, ``every monday 09:00``

Anything else raises :class:`ScheduleError`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_WEEKDAYS = {
    "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
    "friday": 5, "saturday": 6, "sunday": 0,
}
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_MAX_LOOKAHEAD_MINUTES = 366 * 24 * 60  # ~1 year safety bound


class ScheduleError(ValueError):
    """Raised when a schedule spec cannot be parsed."""


@dataclass(frozen=True)
class Schedule:
    kind: str            # "interval" | "cron" | "once"
    one_shot: bool
    interval_seconds: Optional[int] = None
    once_at: Optional[datetime] = None
    cron_fields: Optional[tuple] = None  # (minutes, hours, doms, months, dows) sets
    # POSIX day-of-month / day-of-week are OR'd when BOTH are restricted (not "*").
    dom_restricted: bool = False
    dow_restricted: bool = False

    def next_run_after(self, after: datetime) -> Optional[datetime]:
        """Return the next fire time strictly after ``after`` (None if none)."""
        if self.kind == "interval":
            return after + timedelta(seconds=self.interval_seconds)
        if self.kind == "once":
            return self.once_at if self.once_at > after else None
        if self.kind == "cron":
            return self._next_cron(after)
        raise ScheduleError(f"unknown schedule kind: {self.kind}")

    def _next_cron(self, after: datetime) -> Optional[datetime]:
        minutes, hours, doms, months, dows = self.cron_fields
        dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(_MAX_LOOKAHEAD_MINUTES):
            cron_dow = dt.isoweekday() % 7  # Mon..Sun(7)->1..0 ; Sunday == 0
            # POSIX rule: when BOTH day-of-month and day-of-week are restricted, the
            # job runs when EITHER matches (union); otherwise AND the restricted one.
            if self.dom_restricted and self.dow_restricted:
                day_match = (dt.day in doms) or (cron_dow in dows)
            elif self.dom_restricted:
                day_match = dt.day in doms
            elif self.dow_restricted:
                day_match = cron_dow in dows
            else:
                day_match = True
            if (dt.minute in minutes and dt.hour in hours
                    and dt.month in months and day_match):
                return dt
            dt += timedelta(minutes=1)
        return None


def parse_schedule(spec: str) -> Schedule:
    if not spec or not spec.strip():
        raise ScheduleError("empty schedule spec")
    raw = spec.strip()
    low = raw.lower()

    # every-phrase
    if low.startswith("every "):
        return _parse_every(low[len("every "):].strip())

    # duration interval
    m = _DURATION_RE.match(low)
    if m:
        seconds = int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
        if seconds <= 0:
            raise ScheduleError(f"non-positive interval: {spec}")
        return Schedule(kind="interval", one_shot=False, interval_seconds=seconds)

    # 5-field cron
    if len(raw.split()) == 5:
        return _parse_cron(raw)

    # ISO timestamp (one-shot)
    try:
        once_at = datetime.fromisoformat(raw)
    except ValueError:
        raise ScheduleError(f"unrecognized schedule spec: {spec!r}")
    # Normalize a tz-aware input to naive LOCAL time so it compares against the
    # scheduler's naive datetime.now() — a tz-aware once_at otherwise raised
    # "can't compare offset-naive and offset-aware datetimes" out of next_run_after.
    if once_at.tzinfo is not None:
        once_at = once_at.astimezone().replace(tzinfo=None)
    return Schedule(kind="once", one_shot=True, once_at=once_at)


def _parse_every(rest: str) -> Schedule:
    # "every 15m" -> interval
    m = _DURATION_RE.match(rest)
    if m:
        seconds = int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
        if seconds <= 0:
            # A zero/negative interval makes every scheduler tick see the job as due,
            # re-running it forever (runaway cost). Reject like the bare-duration path.
            raise ScheduleError(f"interval must be positive: every {rest!r}")
        return Schedule(kind="interval", one_shot=False, interval_seconds=seconds)

    parts = rest.split()
    if len(parts) != 2:
        raise ScheduleError(f"unrecognized 'every' phrase: every {rest!r}")
    when, time_str = parts
    hm = _HHMM_RE.match(time_str)
    if not hm:
        raise ScheduleError(f"bad time in 'every' phrase: {time_str!r}")
    hour, minute = int(hm.group(1)), int(hm.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ScheduleError(f"time out of range: {time_str!r}")

    if when == "day":
        dows = set(range(7))
        dow_restricted = False  # every day -> DOW not restricted
    elif when in _WEEKDAYS:
        dows = {_WEEKDAYS[when]}
        dow_restricted = True   # every <weekday> -> DOW restricted, honor the set
    else:
        raise ScheduleError(f"unknown day in 'every' phrase: {when!r}")
    fields = ({minute}, {hour}, set(range(1, 32)), set(range(1, 13)), dows)
    return Schedule(kind="cron", one_shot=False, cron_fields=fields,
                    dom_restricted=False, dow_restricted=dow_restricted)


def _parse_cron(raw: str) -> Schedule:
    fields = raw.split()
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    try:
        minutes = _expand(fields[0], *ranges[0])
        hours = _expand(fields[1], *ranges[1])
        doms = _expand(fields[2], *ranges[2])
        months = _expand(fields[3], *ranges[3])
        dows = _expand(fields[4], *ranges[4])
    except ScheduleError:
        raise
    except Exception as e:
        raise ScheduleError(f"bad cron expression {raw!r}: {e}")
    if 7 in dows:  # cron allows 7 for Sunday; normalize to 0
        dows = (dows - {7}) | {0}
    # A field is "restricted" when it is not a bare/stepped wildcard (Vixie rule:
    # first char != '*'), which selects POSIX OR semantics when both are restricted.
    dom_restricted = not fields[2].strip().startswith("*")
    dow_restricted = not fields[4].strip().startswith("*")
    return Schedule(kind="cron", one_shot=False,
                    cron_fields=(minutes, hours, doms, months, dows),
                    dom_restricted=dom_restricted, dow_restricted=dow_restricted)


def _expand(field: str, lo: int, hi: int) -> set:
    """Expand one cron field into a set of ints. Supports * , - and */n."""
    result: set = set()
    for part in field.split(","):
        step = 1
        body = part
        if "/" in part:
            body, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ScheduleError(f"bad step in {field!r}")
        if body == "*":
            start, end = lo, hi
        elif "-" in body:
            a, b = body.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(body)
        if start < lo or end > hi or start > end:
            raise ScheduleError(f"cron field out of range: {part!r} (allowed {lo}-{hi})")
        result.update(range(start, end + 1, step))
    if not result:
        raise ScheduleError(f"empty cron field: {field!r}")
    return result
