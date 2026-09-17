"""The goal board's vocabulary — status, kind, ask and objective literals.

The board (``agents/task/goals/board.py``) owns the semantics; the LITERALS
live here, in the core tier, because five modules below and beside it need
them and ``core/`` may not import ``agents.*``: the status snapshot and the
board renderer re-declared them by hand (pinned equal by a contract test),
and the live-status set was spelled out three times (streams, goal_list, the
snapshot). One spelling, imported everywhere.
"""
from __future__ import annotations

# goal lifecycle
STATUS_TRIAGE = "triage"
STATUS_WAITING = "waiting"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

#: Work that is still on the board, in the order a listing shows it.
#: ``done``/``cancelled`` are history and dwarf the live rows within days
#: (357 done vs 1 ready on prod, 2026-08-29).
LIVE_STATUS_ORDER = (STATUS_TRIAGE, STATUS_WAITING, STATUS_READY,
                     STATUS_RUNNING, STATUS_BLOCKED)
LIVE_STATUSES = frozenset(LIVE_STATUS_ORDER)

# kind: rows are goals (dispatchable), objectives (standing, never dispatched),
# or asks (owner-facing needs, never dispatched)
KIND_GOAL = "goal"
KIND_OBJECTIVE = "objective"
KIND_ASK = "ask"

# ask lifecycle — disjoint from goal statuses so nothing dispatches them
ASK_OPEN = "open"
ASK_FULFILLED = "fulfilled"
ASK_REJECTED = "rejected"
ASK_OBSOLETE = "obsolete"

# objective lifecycle (disjoint from goal statuses so nothing dispatches them)
OBJ_ACTIVE = "active"
OBJ_PAUSED = "paused"
OBJ_DROPPED = "dropped"
OBJ_DONE = "done"
