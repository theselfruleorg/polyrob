"""080 WS-2 — the owner's goal verbs, ONE rule for every seat.

``/goal pause``, ``/goal retry`` (Telegram, the REPL, ``polyrob goals``, the
console's Work page) and the one sentence ``/goal show`` prints all come from
here, so a seat cannot drift from another. ``stop`` lives beside the
suppression store it writes (``core.goal_suppressions.stop_goal``).

* **pause** writes ``paused`` — the owner's hold — never ``blocked`` (blocked
  means "it failed"; every view then said "retry after the cause is fixed").
* **retry** is the ONE re-queue verb (``/goal ready`` and ``/goal resume`` are
  deleted): from ``blocked`` it clears the failures; from ``paused`` it lifts
  the hold (to ``waiting`` while a prerequisite is unfinished, so a pause never
  skips a dependency); on a ``ready`` goal an open ask holds, it closes the
  dispatcher's own "Unblock goal" ask, or NAMES the owner decision it waits on.

⚠️ Layering: core may not import ``agents.*`` — the board is injected (any
object with ``get``/``update_status``/``dependencies``/``holding_asks``/
``release_auto_unblock_asks``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from core.goal_legibility import status_reason
from core.goal_vocab import (
    STATUS_BLOCKED, STATUS_DONE, STATUS_PAUSED, STATUS_READY,
    STATUS_RUNNING, STATUS_TRIAGE, STATUS_WAITING,
)

logger = logging.getLogger(__name__)

#: Every goal verb an owner seat offers. ``new`` creates; the rest act on one row.
OWNER_GOAL_VERBS = ("new", "show", "retry", "pause", "stop", "allow")

#: ``payload`` key of the dispatcher's own "Unblock goal: …" ask
#: (``agents.task.goals.board_types.ASK_AUTO_UNBLOCK`` — core may not import it;
#: pinned equal by tests/unit/core/test_goal_owner_verbs.py).
_AUTO_UNBLOCK = "auto_unblock"

_PAUSABLE = (STATUS_TRIAGE, STATUS_WAITING, STATUS_READY, STATUS_RUNNING, STATUS_BLOCKED)


@dataclass(frozen=True)
class VerbResult:
    """``ok`` = the verb did what it says; ``status`` = the row's status after;
    ``message`` = the one owner sentence (no emoji: each seat adds its own mark)."""
    ok: bool
    status: Optional[str]
    message: str


def _gid(goal: Any) -> str:
    return str(getattr(goal, "id", "") or "")[:8]


def holding_asks(board: Any, goal: Any, user_id: str) -> List[Any]:
    """The open asks that name *goal* — ``[]`` when the board cannot say."""
    fn = getattr(board, "holding_asks", None)
    if fn is None:
        return []
    try:
        return list(fn(goal.id, user_id=user_id) or [])
    except Exception:
        logger.debug("holding asks unreadable for %s", getattr(goal, "id", "?"),
                     exc_info=True)
        return []


def reason(board: Any, goal: Any, user_id: str) -> str:
    """The one-sentence status reason for *goal* (asks read from *board*)."""
    status = getattr(goal, "status", "")
    asks = holding_asks(board, goal, user_id) if status in (
        STATUS_READY, STATUS_BLOCKED) else []
    return status_reason(goal, asks)


def pause_goal(board: Any, *, user_id: str, goal: Any) -> VerbResult:
    gid, status = _gid(goal), goal.status
    if status == STATUS_PAUSED:
        return VerbResult(True, status, f"{gid} is already paused. Resume it: /goal retry {gid}")
    if status not in _PAUSABLE:
        return VerbResult(False, status, f"{gid} is {status} — there is nothing to pause.")
    if not board.update_status(goal.id, STATUS_PAUSED, user_id=user_id):
        return VerbResult(False, status, f"Could not pause {gid} — see /goals.")
    note = (" It is running right now — this run finishes, and no new one starts."
            if status == STATUS_RUNNING else "")
    return VerbResult(True, STATUS_PAUSED,
                      f"{gid} paused by you.{note} Resume it: /goal retry {gid}")


def _unfinished_deps(board: Any, goal: Any) -> List[str]:
    try:
        deps = board.dependencies(goal.id) or []
    except Exception:
        return []
    out = []
    for d in deps:
        try:
            row = board.get(d)
        except Exception:
            row = None
        if row is not None and getattr(row, "status", None) != STATUS_DONE:
            out.append(d)
    return out


def retry_goal(board: Any, *, user_id: str, goal: Any) -> VerbResult:
    gid, status = _gid(goal), goal.status
    if status == STATUS_BLOCKED:
        if not board.update_status(goal.id, STATUS_READY, reset_failures=True,
                                   user_id=user_id):
            return VerbResult(False, status, f"Could not retry {gid} — see /goals.")
        return VerbResult(True, STATUS_READY, f"{gid} → ready (failures cleared).")
    if status == STATUS_PAUSED:
        target = STATUS_WAITING if _unfinished_deps(board, goal) else STATUS_READY
        if not board.update_status(goal.id, target, reset_failures=True, user_id=user_id):
            return VerbResult(False, status, f"Could not resume {gid} — see /goals.")
        tail = (" It runs after the goal it depends on." if target == STATUS_WAITING
                else "")
        return VerbResult(True, target, f"{gid} → {target} (pause lifted).{tail}")
    if status == STATUS_READY:
        asks = holding_asks(board, goal, user_id)
        if not asks:
            return VerbResult(False, status, f"{gid} is already queued — it waits its turn.")
        owner = [a for a in asks if not (getattr(a, "payload", None) or {}).get(_AUTO_UNBLOCK)]
        if owner:
            # An owner decision (a tool approval, a need the agent filed) holds
            # it: retry must not answer that decision for him — name it.
            return VerbResult(False, status, f"{gid} is queued but held. "
                              + status_reason(goal, owner))
        try:
            board.release_auto_unblock_asks(goal.id)
        except Exception as exc:
            return VerbResult(False, status, f"Could not release {gid} "
                              f"({type(exc).__name__}) — see /asks.")
        if holding_asks(board, goal, user_id):
            return VerbResult(False, status, f"Could not release {gid} — see /asks.")
        return VerbResult(True, STATUS_READY,
                          f"{gid} → ready (my own unblock ask closed).")
    if status == STATUS_WAITING:
        # 2026-09-08: `waiting` is the ordinary state of a seeded chain. Forcing
        # it would skip the dependency; a bare "no" was a dead end. Explain.
        pending = _unfinished_deps(board, goal)
        if not pending:
            return VerbResult(False, status, (
                f"{gid} is waiting, but nothing unfinished blocks it — it looks "
                f"stranded and should be picked up on the next sweep. "
                f"Detail: /goal show {gid}"))
        lines = [f"{gid} is waiting — it runs after:"]
        for dep_id in pending:
            dep = board.get(dep_id)
            lines.append(f"  • {dep_id[:8]} [{getattr(dep, 'status', '?')}] "
                         f"{getattr(dep, 'title', '')}")
        lines.append(f"That runs first, then this one is picked up automatically. "
                     f"Detail: /goal show {gid}")
        return VerbResult(False, status, "\n".join(lines))
    return VerbResult(False, status, f"{gid} is {status} — there is nothing to retry.")


__all__ = ["OWNER_GOAL_VERBS", "VerbResult", "holding_asks", "pause_goal", "reason",
           "retry_goal"]
