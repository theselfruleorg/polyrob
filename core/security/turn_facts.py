"""The ONE owner-seat execution context (W5, D3).

:func:`owner_seat_context` is the ONE constructor of an owner-seat execution
context (Telegram ``/send`` ``/bridge`` ``/lp`` and the token verbs, the CLI
wallet verbs, the console token routes, the HTTP ``/execute`` routes). Its
shape is the main agent at the top of a turn (``role="orchestrator"``,
``turn_kind="owner_seat"``), with a fresh ``turn_id`` per seat call. Before W5
each surface hand-built an execution context in three shapes.

⚠️ ``metadata["seat"]`` is NOT stamped here: ``"terminal"`` there is the host-shell
seat (``core.security.host_execution.HOST_SEAT``), stamped only by the agent's
own step loop. A seat context names its seat under ``owner_seat``.

Tier-0: imports only ``core``.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Optional

#: The turn kind every owner-seat context carries.
OWNER_SEAT_KIND = "owner_seat"

#: The owner seats (where an authenticated owner typed a verb). ``owner`` = a
#: reply shared by several seats (``/wallet trust`` on Telegram and the REPL).
SEATS = ("telegram", "cli", "repl", "console", "api", "owner")


def owner_seat_context(user_id: Any, *, seat: str, turn_id: Optional[str] = None,
                       session_id: Optional[str] = None) -> SimpleNamespace:
    """The execution context of ONE owner-seat verb call.

    *seat* names the authenticated channel (:data:`SEATS`). Each call is its own
    turn: the replay keys hash the turn id, and a seat call with none was
    'replay blocked' forever on Solana (2026-09-27). The caller authenticated the
    owner; this context never elevates — every gate still checks the principal.
    """
    seat = str(seat or "")
    if seat not in SEATS:
        raise ValueError(f"unknown owner seat {seat!r}")
    ctx = SimpleNamespace(
        user_id=str(user_id or ""), role="orchestrator", is_sub_agent=False,
        metadata={"turn_kind": OWNER_SEAT_KIND, "owner_seat": seat,
                  "turn_id": turn_id or f"{seat}-{uuid.uuid4().hex}"})
    if session_id:
        ctx.session_id = session_id
    return ctx


__all__ = ["OWNER_SEAT_KIND", "SEATS", "owner_seat_context"]
