"""Room membership for the console's Socket.IO rooms (098 TS-22).

The session-room watcher used to stop when a hand-kept COUNTER reached 0. A
``disconnect`` that ran while ``join_session`` awaited ``enter_room`` found no
membership yet and returned; the join then counted a dead socket, the counter
never reached 0, and the session's feed watcher ran for the process life.

Membership is now a SET per room (a count is ``len``, so a double leave or a
missed join cannot drift), and a ``disconnect`` marks the sid dead FIRST, so a
join that resumes after an await sees it and undoes itself.
"""
from __future__ import annotations

from collections import OrderedDict

_DEAD_MAX = 4096
_dead: "OrderedDict[str, None]" = OrderedDict()


def mark_dead(sid: str) -> None:
    _dead[sid] = None
    _dead.move_to_end(sid)
    while len(_dead) > _DEAD_MAX:
        _dead.popitem(last=False)


def is_dead(sid: str) -> bool:
    return sid in _dead


def add(members: dict, room: str, sid: str) -> None:
    members.setdefault(room, set()).add(sid)


def remove(members: dict, room: str, sid: str) -> bool:
    """Remove ``sid``; True when the room is now empty (and dropped)."""
    group = members.get(room)
    if group is not None:
        group.discard(sid)
    if not group:
        members.pop(room, None)
        return True
    return False
