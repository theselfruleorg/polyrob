"""Schedule parsing + next-run computation — ONE parser for cron and rails.

Pure and tz-naive (local wall-clock) so it is deterministic and unit-testable —
every entry point takes the reference time explicitly. Supported spec formats:

- duration interval ............ ``30m``, ``2h``, ``1d``, ``45s`` (also ``every 15m``)
- anchored interval ............ ``every 3d from 2026-10-26T06:59Z`` (fires at
  anchor + k·interval; a fixed N-day cycle that never resets at month end)
- one-shot ISO timestamp ....... ``2026-06-07T09:30:00``
- 5-field cron ................. ``*/15 * * * *`` (min hour dom mon dow; dow 0/7=Sun)
- every-phrase ................. ``every day 14:30``, ``every monday 09:00``

Anything else raises :class:`ScheduleError`.

Lives in ``core/`` (moved from ``cron/schedule.py``, 036) so the goal board's
rails (``agents/task/goals/rails.py``) and the cron subsystem share ONE owner
schedule vocabulary without an agents -> cron import. ``cron.schedule``
re-exports it unchanged.
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
    #: An anchored interval fires at ``anchor + k·interval`` (never drifts with
    #: the run time, never resets at month end). None = from each run.
    anchor: Optional[datetime] = None

    def next_run_after(self, after: datetime) -> Optional[datetime]:
        """Return the next fire time strictly after ``after`` (None if none)."""
        if self.kind == "interval":
            if self.anchor is not None:
                if after < self.anchor:
                    return self.anchor
                step = timedelta(seconds=self.interval_seconds)
                return self.anchor + step * ((after - self.anchor) // step + 1)
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
            else:
                # A wildcard prefix selects AND semantics, not "every day":
                # */2 still has an expanded set that must filter the date.
                day_match = (dt.day in doms) and (cron_dow in dows)
            if (dt.minute in minutes and dt.hour in hours
                    and dt.month in months and day_match):
                return dt
            dt += timedelta(minutes=1)
        return None


def next_runs(schedule: Schedule, after: datetime, n: int = 3) -> List[datetime]:
    """The next *n* fire times strictly after *after* (fewer for a one-shot)."""
    out: List[datetime] = []
    at = after
    for _ in range(max(0, int(n))):
        nxt = schedule.next_run_after(at)
        if nxt is None:
            break
        out.append(nxt)
        at = nxt
    return out


def dom_step_note(spec: str) -> str:
    """A note for a 5-field cron whose DAY-OF-MONTH field steps (``*/3``,
    ``2-31/3``), else ``""``. Such a step restarts on the 1st of every month,
    so a "every 3 days" job fires on the 31st and again on the 1st (prod
    2026-10-31/11-01). The anchored form keeps a fixed N-day cycle."""
    fields = str(spec or "").split()
    if len(fields) != 5 or "/" not in fields[2]:
        return ""
    step = fields[2].rsplit("/", 1)[1]
    n = step if step.isdigit() else "N"
    return (f"⚠ day-of-month step '{fields[2]}' restarts on the 1st of every month "
            f"(a month with 31 days fires on the 31st AND the 1st), so it is not a "
            f"fixed {n}-day cycle. For that, use 'every {n}d from <ISO time>' "
            f"(e.g. 'every {n}d from 2026-10-26T18:00Z'); two jobs of one cycle share "
            f"the anchor plus an offset.")


def shift_spec(spec: str, minutes: int) -> Optional[str]:
    """*spec* moved *minutes* later, or None when it has no fixed slot to move or
    the move would change its day (a bare duration counts from each run; a
    one-shot is a promise about one time)."""
    raw = str(spec or "").strip()
    low = raw.lower()
    if low.startswith("every "):
        return _shift_every(low[len("every "):].strip(), minutes)
    fields = raw.split()
    if len(fields) == 5:
        return _shift_cron(fields, minutes)
    return None


def _shift_every(rest: str, minutes: int) -> Optional[str]:
    if " from " in f" {rest} ":
        head, _, anchor = rest.partition(" from ")
        try:
            at = datetime.fromisoformat(anchor.strip().upper())
        except ValueError:
            return None
        return f"every {head.strip()} from {(at + timedelta(minutes=minutes)).isoformat()}"
    parts = rest.split()
    hm = _HHMM_RE.match(parts[1]) if len(parts) == 2 else None
    if not hm:
        return None
    total = int(hm.group(1)) * 60 + int(hm.group(2)) + minutes
    if total >= 24 * 60:
        return None
    return f"every {parts[0]} {total // 60:02d}:{total % 60:02d}"


def _shift_cron(fields: List[str], minutes: int) -> Optional[str]:
    minute, hour = fields[0], fields[1]
    if minute.startswith("*/") and minute[2:].isdigit():
        return (" ".join([f"{minutes}-59/{minute[2:]}"] + fields[1:])
                if minutes < int(minute[2:]) else None)
    values = minute.split(",")
    if not all(v.isdigit() for v in values):
        return None
    moved = [int(v) + minutes for v in values]
    if all(m < 60 for m in moved):
        return " ".join([",".join(str(m) for m in moved)] + fields[1:])
    if hour == "*":
        return " ".join([",".join(str(m % 60) for m in moved)] + fields[1:])
    hours = hour.split(",")
    if len(moved) != 1 or not all(h.isdigit() for h in hours):
        return None
    new_hours = [int(h) + moved[0] // 60 for h in hours]
    if max(new_hours) > 23:
        return None
    return " ".join([str(moved[0] % 60), ",".join(str(h) for h in new_hours)] + fields[2:])


def _parse_anchor(text: str) -> datetime:
    try:
        at = datetime.fromisoformat(text.strip().upper())
    except ValueError:
        raise ScheduleError(f"bad anchor time {text!r}: use an ISO time, "
                            f"e.g. 2026-10-26T06:59Z")
    if at.tzinfo is not None:  # naive LOCAL, like a one-shot (see parse_schedule)
        at = at.astimezone().replace(tzinfo=None)
    return at


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
    # "every 3d from 2026-10-26T06:59Z" -> anchored interval
    if " from " in f" {rest} ":
        head, _, anchor = f" {rest} ".partition(" from ")
        base = _parse_every(head.strip()) if head.strip() else None
        if base is None or base.kind != "interval" or not anchor.strip():
            raise ScheduleError(f"an anchored interval reads 'every <30m|2h|3d> from "
                                f"<ISO time>', not every {rest!r}")
        return Schedule(kind="interval", one_shot=False,
                        interval_seconds=base.interval_seconds,
                        anchor=_parse_anchor(anchor))
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
