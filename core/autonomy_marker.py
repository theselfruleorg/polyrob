"""In-process registry of AUTONOMOUS sessions (goal/cron/planner-spawned).

Server-side only — an agent cannot mark or unmark a session. Used by the goal
tool to refuse objective/goal mutations from autonomous runs (a runaway goal
must never rewrite its own mission). In-process is sufficient: goal/cron runs
execute in the same process as their dispatcher, and claims don't survive a
restart anyway.

Each entry also remembers WHICH goal the session is running (039). The approval
queue needs it: an owner-queue ask raised by a goal run must stamp
``blocks_goal_ids`` so that `GoalBoard.decide_ask`'s existing unblock hop re-arms
that goal when the owner approves. Without it the owner presses /approve and
nothing happens — the grant sits unredeemed and the goal stays blocked, which is
the same "owner intent does not stick" failure one layer down.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Optional

_MAX = 512
#: session_id -> the goal id it is running (or None for a cron/planner run that
#: has no goal row). Membership is what `is_autonomous` answers; the value is
#: only ever a hint for re-arming, never an authority check.
_SESSIONS: "OrderedDict[str, Optional[str]]" = OrderedDict()
#: session_id -> the cron job id it is running (2026-09-18). The approval queue
#: needs it for the same reason it needs the goal id: an owner-queue ask raised
#: by a cron run must name its job so an approval re-arms THAT job to run on
#: the next tick. Without it the approval woke the finished run's session as a
#: self-wake — a forged turn the money guard refuses — and the agent told the
#: owner to "trigger it from your seat" for a trade the owner had just approved.
_CRON_JOBS: "OrderedDict[str, str]" = OrderedDict()


def mark_autonomous(session_id: str, goal_id: Optional[str] = None,
                    *, cron_job_id: Optional[str] = None) -> None:
    if not session_id:
        return
    _SESSIONS[session_id] = goal_id
    _SESSIONS.move_to_end(session_id)
    # Membership is an authority boundary, not an expendable cache entry.
    # Never make a still-resumable autonomous session look like owner chat.
    if cron_job_id:
        _CRON_JOBS[session_id] = cron_job_id
        _CRON_JOBS.move_to_end(session_id)
        while len(_CRON_JOBS) > _MAX:
            _CRON_JOBS.popitem(last=False)


def is_autonomous(session_id: Optional[str]) -> bool:
    return bool(session_id) and session_id in _SESSIONS


def goal_for_session(session_id: Optional[str]) -> Optional[str]:
    """The goal this autonomous session is running, or None.

    None is a real answer, not a failure: a cron or planner run is autonomous and
    has no goal row. A caller must degrade rather than refuse on it.
    """
    if not session_id:
        return None
    return _SESSIONS.get(session_id)


def cron_job_for_session(session_id: Optional[str]) -> Optional[str]:
    """The cron job this autonomous session is running, or None (a goal or
    planner run, or an interactive session). None is a real answer."""
    if not session_id:
        return None
    return _CRON_JOBS.get(session_id)


#: cron job id -> the job's task text (CHAT-5). A standing instruction
#: ("moderate The Lobby") is the authority an autonomous run carries for
#: that room. Server-side only, like the rest of this registry.
_JOBS: "OrderedDict[str, str]" = OrderedDict()


#: cron job id -> its ``payload.post_spacing_hours`` (packs/x/polyrob_x/post_spacing.py).
_JOB_SPACING: "OrderedDict[str, float]" = OrderedDict()


def note_job(job_id: Optional[str], task: Optional[str], *,
             post_spacing_hours: Any = None) -> None:
    """Record a cron job's task text and post spacing (the cron runner calls this)."""
    if not job_id:
        return
    try:
        spacing = float(post_spacing_hours) if post_spacing_hours is not None else 0.0
    except (TypeError, ValueError):
        spacing = 0.0
    if spacing > 0:
        _JOB_SPACING[job_id] = spacing
        _JOB_SPACING.move_to_end(job_id)
        while len(_JOB_SPACING) > _MAX:
            _JOB_SPACING.popitem(last=False)
    else:
        _JOB_SPACING.pop(job_id, None)
    if not task:
        return
    _JOBS[job_id] = str(task)
    _JOBS.move_to_end(job_id)
    while len(_JOBS) > _MAX:
        _JOBS.popitem(last=False)


def post_spacing_for_job(job_id: Optional[str]) -> Optional[float]:
    """The job's minimum hours between its own posts, or None (no rule)."""
    return _JOB_SPACING.get(job_id) if job_id else None


#: goal id -> the goal's text (the goal dispatcher calls :func:`note_goal`). A
#: standing goal carries the same standing authority as a cron job.
_GOALS: "OrderedDict[str, str]" = OrderedDict()


def note_goal(goal_id: Optional[str], text: Optional[str]) -> None:
    """Record a goal's text (the goal dispatcher calls this)."""
    if not goal_id or not text:
        return
    _GOALS[goal_id] = str(text)
    _GOALS.move_to_end(goal_id)
    while len(_GOALS) > _MAX:
        _GOALS.popitem(last=False)


def job_task_for_session(session_id: Optional[str]) -> Optional[str]:
    """The task text of the cron job or goal this session runs, or None (a
    planner run, an interactive session)."""
    job = cron_job_for_session(session_id)
    if job:
        return _JOBS.get(job)
    goal = goal_for_session(session_id)
    return _GOALS.get(goal) if goal else None


#: 081 WS-3.1: the OWNER's interactive chat seats — a session of the owner
#: principal that is not a room, not a correspondent session and not an
#: autonomous run (``agents.task.session_class.stamp_owner_seat`` decides and
#: stamps it before the session's Controller is built). Such a session runs on a
#: narrow eager rig, so it always pins the ``<tool-catalog>`` + ``load_tool`` and
#: reaches a late tool through the ``tool_call`` bridge — the same shape as a
#: cron run (``tools/late_tool_mode.py``). Not an authority: it grants nothing.
_OWNER_SEATS: "OrderedDict[str, None]" = OrderedDict()
_MAX_SEATS = 4096


def mark_owner_seat(session_id: Optional[str]) -> None:
    if not session_id:
        return
    _OWNER_SEATS[session_id] = None
    _OWNER_SEATS.move_to_end(session_id)
    while len(_OWNER_SEATS) > _MAX_SEATS:
        _OWNER_SEATS.popitem(last=False)


def is_owner_seat(session_id: Optional[str]) -> bool:
    return bool(session_id) and session_id in _OWNER_SEATS
