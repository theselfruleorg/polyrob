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


def mark_autonomous(session_id: str, goal_id: Optional[str] = None) -> None:
    if not session_id:
        return
    _SESSIONS[session_id] = goal_id
    _SESSIONS.move_to_end(session_id)
    while len(_SESSIONS) > _MAX:
        _SESSIONS.popitem(last=False)


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
