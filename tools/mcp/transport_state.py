"""MCP transport liveness (098 TS-02): OPEN -> PEER_LOST -> CLOSED.

Peer death (stdio EOF, process exit, SSE stream end) is a state, not "no
message yet": it moves the transport to PEER_LOST, ``is_closed`` reads True,
the client's message loop exits and every pending request fails with an
exception (never ``cancel()``, which would read as the CALLER's cancellation).
"""
from __future__ import annotations

from typing import Any, Dict

from core.state.machine import StateMachine

OPEN, PEER_LOST, CLOSED = "open", "peer_lost", "closed"

TRANSPORT = StateMachine(
    "mcp.transport",
    states={OPEN, PEER_LOST, CLOSED},
    transitions={OPEN: {PEER_LOST, CLOSED}, PEER_LOST: {CLOSED}},
    terminal={CLOSED},
    initial={OPEN},
)


def mark(transport: Any, to: str) -> None:
    """Move ``transport`` to ``to``; a repeat or a late PEER_LOST after a
    local close is a no-op (not an illegal edge)."""
    frm = getattr(transport, "_tstate", OPEN)
    if frm == to or (frm == CLOSED and to == PEER_LOST):
        return
    if TRANSPORT.check(frm, to, ctx={"transport": type(transport).__name__}):
        transport._tstate = to


def fail_pending(pending: Dict[Any, Any], exc: BaseException) -> int:
    """Fail every unfinished future in ``pending`` with ``exc``; clear the map."""
    n = 0
    for fut in list(pending.values()):
        if not fut.done():
            fut.set_exception(exc)
            fut.exception()  # mark retrieved: a waiter may already be gone
            n += 1
    pending.clear()
    return n


def fail_future(fut: Any, exc: BaseException) -> None:
    if fut is not None and not fut.done():
        fut.set_exception(exc)
        fut.exception()
