"""The goal board's two status machines (098 S1): goal rows and owner asks.

Both live in ``goals.db::goals`` and are told apart by ``kind``. Every status
write in ``board.py`` / ``board_asks.py`` / ``board_deps.py`` names its edge in
one of these tables; owner edges (pause, retry, unblock, cancel) are listed
like every other edge, so a stale snapshot can never ask for one that is not.
"""
from __future__ import annotations

from core.goal_vocab import (
    ASK_FULFILLED, ASK_OBSOLETE, ASK_OPEN, ASK_REJECTED,
    STATUS_BLOCKED, STATUS_CANCELLED, STATUS_DONE, STATUS_PAUSED, STATUS_READY,
    STATUS_RUNNING, STATUS_TRIAGE, STATUS_WAITING,
)
from core.state.machine import StateMachine

#: ``dep_failed`` is a ``block_kind`` on a ``blocked`` row, not a status.
GOAL_MACHINE = StateMachine(
    name="goal",
    states=frozenset({STATUS_TRIAGE, STATUS_WAITING, STATUS_READY, STATUS_RUNNING,
                      STATUS_BLOCKED, STATUS_PAUSED, STATUS_DONE, STATUS_CANCELLED}),
    transitions={
        STATUS_TRIAGE: frozenset({STATUS_READY, STATUS_WAITING, STATUS_BLOCKED,
                                  STATUS_PAUSED, STATUS_CANCELLED}),
        STATUS_WAITING: frozenset({STATUS_READY, STATUS_BLOCKED, STATUS_PAUSED,
                                   STATUS_CANCELLED}),
        STATUS_READY: frozenset({STATUS_RUNNING, STATUS_BLOCKED, STATUS_PAUSED,
                                 STATUS_CANCELLED}),
        STATUS_RUNNING: frozenset({STATUS_DONE, STATUS_READY, STATUS_BLOCKED,
                                   STATUS_PAUSED, STATUS_CANCELLED}),
        STATUS_BLOCKED: frozenset({STATUS_READY, STATUS_PAUSED, STATUS_CANCELLED}),
        STATUS_PAUSED: frozenset({STATUS_READY, STATUS_WAITING, STATUS_CANCELLED}),
    },
    terminal=frozenset({STATUS_DONE, STATUS_CANCELLED}),
    initial=frozenset({STATUS_TRIAGE, STATUS_READY, STATUS_WAITING, STATUS_BLOCKED}),
)

#: An owner ask: open until the owner decides it, or the need goes away
#: (``obsolete`` — superseded, its goals resolved, or ``expires_at`` passed).
ASK_MACHINE = StateMachine(
    name="owner.ask",
    states=frozenset({ASK_OPEN, ASK_FULFILLED, ASK_REJECTED, ASK_OBSOLETE}),
    transitions={ASK_OPEN: frozenset({ASK_FULFILLED, ASK_REJECTED, ASK_OBSOLETE})},
    terminal=frozenset({ASK_FULFILLED, ASK_REJECTED, ASK_OBSOLETE}),
)

__all__ = ["ASK_MACHINE", "GOAL_MACHINE"]
