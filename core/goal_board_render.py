"""Render the goal board so the owner can see WHY nothing is moving.

The old ``/goals`` reply was status counts plus the five most recent open rows.
On 2026-09-08 that showed the owner::

    a66f5f92 [ready]    Treasury: refresh the watchlist
    2dee2891 [waiting]  Treasury: manage open positions and take an entry
    709ec00f [waiting]  Treasury: publish the track record

Three unrelated-looking lines, two of them the bare word ``waiting``. Nothing
said that goal 2 was parked because goal 1 had not run yet — though
``goal_edges`` has held exactly that fact the whole time. The owner asked "so
the trading goal is running?" and could not answer it from the surface. That
question is the requirement.

Two changes, both cheap because the data already exists:

* A ``waiting`` row names its blocking prerequisite, by id AND title. An 8-char
  hex id is not something a human can reason about on a phone.
* What needs a human leads. ``running`` first (this is what "is it working
  right now" means), then ``blocked`` (this is what needs you), then the queue.

Pure and board-shaped: it touches only ``status_counts`` / ``list_recent`` /
``dependencies`` / ``get``, so it is testable with a fake and reusable by any
seat. **Never** ``board.list`` — that is ``ORDER BY priority DESC LIMIT`` and a
window over it evicts the newest low-priority rows, which is how the agent's own
``goal_list`` showed the oldest 100 rows and zero stream legs on 2026-08-29.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Order the owner reads in: what is live, then what wants them, then the queue.
_LEAD_ORDER = ("running", "blocked", "triage", "ready", "waiting")

#: Enough rows to be useful on a phone without becoming a wall.
_MAX_SHOWN = 12


def _title(goal: Any) -> str:
    return (getattr(goal, "title", None) or "(untitled)").strip()


#: D8: what ``_blocking`` returns when it could not READ the edges. An empty
#: list means "no unfinished prerequisite", which the renderer reports as
#: STRANDED — a specific, actionable diagnosis. A read fault is not that
#: diagnosis, and rendering it as one told the owner a goal needed a janitor
#: sweep when in fact nothing had been read at all.
class _Unreadable:
    """Sentinel: the dependency edges could not be read. Carries the reason."""

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __bool__(self) -> bool:      # an unreadable answer is not "no blockers"
        return False


def _blocking(board: Any, goal: Any):
    """Prerequisites of ``goal`` that are not done yet.

    Returns a list, or :class:`_Unreadable` when the edge read itself failed.
    Never raises — a board view without edges beats a board view that 500s, but
    it must SAY it has no edges rather than imply there are none.
    """
    try:
        dep_ids = board.dependencies(getattr(goal, "id", "")) or []
    except Exception as exc:
        logger.debug("dependency read failed; rendering without edges",
                     exc_info=True)
        return _Unreadable(f"{type(exc).__name__}: {str(exc)[:80]}")
    out = []
    unreadable = 0
    for dep_id in dep_ids:
        try:
            dep = board.get(dep_id)
        except Exception:
            unreadable += 1
            continue
        if dep is not None and getattr(dep, "status", None) != "done":
            out.append(dep)
    if unreadable and not out:
        # Every prerequisite we could name failed to load. "No unfinished
        # prerequisite" would be a confident lie about rows nobody read.
        return _Unreadable(f"{unreadable} prerequisite row(s) unreadable")
    return out


def render_board(board: Any, *, user_id: Optional[str] = None) -> str:
    """One owner-readable board summary. Names no action the owner cannot take."""
    # D7: an unreadable board is not an EMPTY board. This used to swallow the
    # read fault into `counts = {}` and answer "No goals yet." — the most
    # reassuring sentence available, over a store nobody could open.
    try:
        counts = board.status_counts(user_id=user_id) or {}
    except Exception as exc:
        logger.warning("goal board status_counts failed", exc_info=True)
        return (f"Goal board: unavailable ({type(exc).__name__}: "
                f"{str(exc)[:120]}) — this is UNKNOWN, not an empty board.")
    total = sum(counts.values())
    if not total:
        return "No goals yet."

    lines = [f"{total} goal(s): " +
             ", ".join(f"{s}={n}" for s, n in sorted(counts.items()))]

    try:
        rows = board.list_recent(user_id=user_id, statuses=_LEAD_ORDER,
                                 limit=_MAX_SHOWN * 2) or []
    except Exception as exc:
        logger.warning("goal board list_recent failed", exc_info=True)
        lines.append(f"The open rows are unavailable ({type(exc).__name__}: "
                     f"{str(exc)[:80]}) — the counts above are still true.")
        return "\n".join(lines)
    if not rows:
        return "\n".join(lines)

    rank = {s: i for i, s in enumerate(_LEAD_ORDER)}
    rows = sorted(rows, key=lambda g: rank.get(getattr(g, "status", ""), 99))

    lines.append("")
    for goal in rows[:_MAX_SHOWN]:
        gid = (getattr(goal, "id", "") or "")[:8]
        status = getattr(goal, "status", "?")
        lines.append(f"• {gid} [{status}] {_title(goal)}")
        if status == "waiting":
            blockers = _blocking(board, goal)
            if isinstance(blockers, _Unreadable):
                lines.append("    ↳ waiting — its prerequisites are "
                             f"unavailable ({blockers.reason}); I cannot tell "
                             "whether it is queued or stranded")
                continue
            for dep in blockers:
                dep_id = (getattr(dep, "id", "") or "")[:8]
                lines.append(f"    ↳ waiting on {dep_id} "
                             f"[{getattr(dep, 'status', '?')}] {_title(dep)}")
            if not blockers:
                # Honest about the one case that looks identical from outside:
                # a waiting row whose prerequisites are all done is stranded,
                # not queued, and the janitor (reconcile_waiting) owns it.
                lines.append("    ↳ waiting, but no unfinished prerequisite — "
                             "stranded; it should be picked up on the next sweep")

    hidden = len(rows) - min(len(rows), _MAX_SHOWN)
    if hidden > 0:
        lines.append(f"…and {hidden} more open goal(s).")
    lines.append("")
    lines.append("Detail: /goal show <id> · asks: /asks · approvals: /pending")
    return "\n".join(lines)


__all__ = ["render_board"]
