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
from typing import Optional

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


#: cron job id -> the job's task text, recorded ONLY for an OWNER-authored job
#: (CHAT-5). A standing owner instruction ("moderate The Public Den") is the
#: authority an autonomous run carries for that room; an agent-authored job
#: carries none. Server-side only, like the rest of this registry.
_OWNER_JOBS: "OrderedDict[str, str]" = OrderedDict()


def note_owner_job(job_id: Optional[str], task: Optional[str]) -> None:
    """Record an owner-authored cron job's task text (the cron runner calls this)."""
    if not job_id or not task:
        return
    _OWNER_JOBS[job_id] = str(task)
    _OWNER_JOBS.move_to_end(job_id)
    while len(_OWNER_JOBS) > _MAX:
        _OWNER_JOBS.popitem(last=False)


#: goal id -> the goal's text, recorded ONLY for an OWNER-authored goal (the
#: goal dispatcher calls :func:`note_owner_goal`). An owner-authored standing
#: goal carries the same standing authority as an owner-authored cron job; an
#: agent-authored (or unstamped) goal is never recorded.
_OWNER_GOALS: "OrderedDict[str, str]" = OrderedDict()


def note_owner_goal(goal_id: Optional[str], text: Optional[str]) -> None:
    """Record an owner-authored goal's text (the goal dispatcher calls this)."""
    if not goal_id or not text:
        return
    _OWNER_GOALS[goal_id] = str(text)
    _OWNER_GOALS.move_to_end(goal_id)
    while len(_OWNER_GOALS) > _MAX:
        _OWNER_GOALS.popitem(last=False)


def forget_owner_goal(goal_id: Optional[str]) -> None:
    """Drop a goal's owner record (a run of an agent-authored goal re-checks)."""
    if goal_id:
        _OWNER_GOALS.pop(goal_id, None)


def owner_job_task_for_session(session_id: Optional[str]) -> Optional[str]:
    """The task text of the OWNER-authored cron job or goal this session runs,
    or None (an agent-authored job or goal, a planner run, an interactive
    session)."""
    job = cron_job_for_session(session_id)
    if job:
        return _OWNER_JOBS.get(job)
    goal = goal_for_session(session_id)
    return _OWNER_GOALS.get(goal) if goal else None
