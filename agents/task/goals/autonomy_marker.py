"""In-process registry of AUTONOMOUS sessions (goal/cron/planner-spawned).

Server-side only — an agent cannot mark or unmark a session. Used by the goal
tool to refuse objective/goal mutations from autonomous runs (a runaway goal
must never rewrite its own mission). In-process is sufficient: goal/cron runs
execute in the same process as their dispatcher, and claims don't survive a
restart anyway.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Optional

_MAX = 512
_SESSIONS: "OrderedDict[str, None]" = OrderedDict()


def mark_autonomous(session_id: str) -> None:
    if not session_id:
        return
    _SESSIONS[session_id] = None
    _SESSIONS.move_to_end(session_id)
    while len(_SESSIONS) > _MAX:
        _SESSIONS.popitem(last=False)


def is_autonomous(session_id: Optional[str]) -> bool:
    return bool(session_id) and session_id in _SESSIONS
