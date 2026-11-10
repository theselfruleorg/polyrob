"""How a scheduled job's last run ended, in one owner line (routines review 2026-10-29).

The scheduler stores ``payload.last_outcome = {"outcome", "at", "reason"?}`` on
the job row (``cron.jobs.CronJobStore.record_outcome``). Every view that names
it — ``/jobs``, ``polyrob job list``, ``cronjob_list``, the agent's
``<operations>`` block — renders it here, so "failed 10-28 18:05 (stopped at
the step limit (150/150))" reads the same on every seat. Pure; core tier, so the
status snapshot can use it without a cron import.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

#: The outcomes of a run that did not do its job; the owner hears of these.
FAILED_OUTCOMES = ("failed", "partial")
#: Failed runs in a row before the scheduler pauses a recurring job (107 D1).
FAILURE_STREAK_LIMIT = 3
#: A job this far past its slot reads as late (107 WS-4).
LATE_AFTER_SECONDS = 1800
#: A money job this far past its slot does not run (107 D4).
LATE_MONEY_SKIP_SECONDS = 3600
#: ``payload.paused.by`` of a job that the failure limit paused.
FAILURE_STREAK_BY = "failure_streak"


def span_text(seconds: float) -> str:
    """``"40m"``, ``"3h"``, ``"2d"``."""
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{minutes}m"
    return f"{minutes // 60}h" if minutes < 48 * 60 else f"{minutes // 1440}d"


def waiting_text(next_run_at: Any, now: Optional[datetime] = None) -> str:
    """``"waiting 40m"`` for a job past its slot by the late limit, else ``""``.
    *next_run_at* may be the stored ISO text; an unreadable value has no wait."""
    if isinstance(next_run_at, str):
        try:
            next_run_at = datetime.fromisoformat(next_run_at)
        except ValueError:
            return ""
    if not isinstance(next_run_at, datetime):
        return ""
    now = now or datetime.now(next_run_at.tzinfo)
    late = (now - next_run_at).total_seconds()
    return f"waiting {span_text(late)}" if late >= LATE_AFTER_SECONDS else ""


def late_task_line(late_seconds: float, now: datetime) -> str:
    """The line at the head of a late run's task text."""
    due = now - timedelta(seconds=late_seconds)
    return (f"[This run is {span_text(late_seconds)} late: it was due "
            f"{due.strftime('%m-%d %H:%M')} and the time is now "
            f"{now.strftime('%m-%d %H:%M')}. Say so if the delay matters.]")


def outcome_text(rec: Optional[dict], last_run_at: Any = None) -> str:
    """``"failed 10-28 18:05 (stopped at the step limit)"``, ``"done 10-13 08:00"``,
    ``"ran 10-13 08:00"`` (a run before outcomes were stored) or ``"never run"``.
    The reason is shown only for a run that did not finish as ``done``."""
    rec = rec if isinstance(rec, dict) else {}
    at = str(rec.get("at") or "")
    when = at[5:16].replace("T", " ") if at else ""
    outcome = str(rec.get("outcome") or "")
    if outcome:
        text = f"{outcome} {when}".strip()
        reason = str(rec.get("reason") or "").strip()
        if reason and outcome != "done":
            text += f" ({reason})"
        if rec.get("late_by"):
            text += f", {span_text(rec['late_by'])} late"
        return text
    if isinstance(last_run_at, datetime):
        return f"ran {last_run_at.strftime('%m-%d %H:%M')}"
    if last_run_at:
        return f"ran {str(last_run_at)[5:16].replace('T', ' ')}"
    return "never run"


def failed_recently(rec: Optional[dict], now: datetime, hours: float = 48.0) -> bool:
    """The last run failed (or ended partial) within *hours* of *now*."""
    rec = rec if isinstance(rec, dict) else {}
    if rec.get("outcome") not in FAILED_OUTCOMES:
        return False
    try:
        at = datetime.fromisoformat(str(rec.get("at")))
    except (TypeError, ValueError):
        return True  # a failure with no readable time is still a failure
    return (now - at).total_seconds() <= hours * 3600


def stopped_by_failures(payload: Optional[dict]) -> bool:
    """True when the failure limit paused this job, not the owner."""
    return ((payload or {}).get("paused") or {}).get("by") == FAILURE_STREAK_BY


def agent_armed(payload: Optional[dict]) -> bool:
    """True when the agent (not an owner seat) CREATED the job.

    ``payload.armed_by`` is written once, at creation. ``payload.actor`` also
    moves when a later edit sets a rig (085 W8 A4: the rig's writer), so it
    cannot say who owns the job; it is read only for rows created before
    ``armed_by`` existed."""
    p = payload or {}
    return (p.get("armed_by") or p.get("actor")) == "agent"
