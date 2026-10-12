"""TurnFacts: what every policy decision knows about a turn, built ONCE (W5, D3).

A policy decision (may this verb run, may this spend happen, is this send held)
needs the same facts on every surface: who the principal is, which seat it came
through, the turn's origin, the taints it holds, the compute posture and the
wallet custody mode. Before W5 each surface hand-built an execution context
(``SimpleNamespace(role="owner", ...)`` in three shapes) and each gate re-derived
the facts from it. Now:

- :func:`owner_seat_context` is the ONE constructor of an owner-seat execution
  context (Telegram ``/send`` ``/bridge`` ``/lp`` and the token verbs, the CLI
  wallet verbs, the console token routes, the HTTP ``/execute`` routes). Its
  shape is the main agent at the top of a turn (``role="orchestrator"``,
  ``turn_kind="owner_seat"``), with a fresh ``turn_id`` per seat call.
- :func:`facts_of` builds the :class:`TurnFacts` of any execution context; the
  origin comes from ``core.security.owner_turn`` and the taints from
  ``core.security.taints`` — never a second derivation.

⚠️ ``metadata["seat"]`` is NOT stamped here: ``"terminal"`` there is the host-shell
seat (``core.security.host_execution.HOST_SEAT``), stamped only by the agent's
own step loop. A seat context names its seat under ``owner_seat``.

Tier-0: imports only ``core``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

from core.security.taints import EMPTY, TaintSet

#: The turn kind every owner-seat context carries.
OWNER_SEAT_KIND = "owner_seat"

#: The owner seats (where an authenticated owner typed a verb). ``owner`` = a
#: reply shared by several seats (``/wallet trust`` on Telegram and the REPL).
SEATS = ("telegram", "cli", "repl", "console", "api", "owner")

#: Turn origins, most-privileged last. ``direct`` = no execution context (an
#: in-process Python call); never the owner (``owner_direct_turn``).
ORIGINS = ("direct", "leaf", "forged", "room", "correspondent", "autonomous", "owner")


@dataclass(frozen=True)
class TurnFacts:
    principal: str = ""
    seat: str = ""
    origin: str = "direct"
    taints: TaintSet = EMPTY
    posture: int = 0
    custody: str = "none"
    session_id: str = ""
    turn_id: str = ""

    @property
    def owner_driving(self) -> bool:
        """The owner is typing now, with no third-party text in the turn."""
        return self.origin == "owner" and not self.taints


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


def _origin(execution_context: Any, controller_self: Any, taint_set: TaintSet) -> str:
    from core.security import owner_turn
    if execution_context is None:
        return "direct"
    try:
        if owner_turn.is_leaf(execution_context, controller_self):
            return "leaf"
        if owner_turn.forged_kind(execution_context):
            return "forged"
        if owner_turn.is_room_turn(execution_context):
            return "room"
        if taint_set.correspondent:
            return "correspondent"
        if owner_turn.is_autonomous_session(execution_context, controller_self):
            return "autonomous"
    except Exception:
        return "forged"   # fail closed: an unprovable turn is not the owner
    return "owner"


def _custody() -> str:
    try:
        from core.security.host_execution import wallet_custody_enabled
        if not wallet_custody_enabled():
            return "none"
        from core.signer import signer_mode
        return str(signer_mode())
    except Exception:
        return "unknown"


def _posture() -> int:
    try:
        from core.config_policy import compute_posture
        return int(compute_posture())
    except Exception:
        return 0


def facts_of(execution_context: Any, *, orchestrator: Any = None,
             controller_self: Any = None, seat: Optional[str] = None) -> TurnFacts:
    """The facts of this turn. Never raises; a fault reads least-privileged."""
    from core.security import taints
    meta = getattr(execution_context, "metadata", None)
    meta = meta if isinstance(meta, dict) else {}
    if orchestrator is None and controller_self is not None:
        orchestrator = getattr(controller_self, "orchestrator", None)
    taint_set = taints.of(orchestrator=orchestrator, execution_context=execution_context)
    return TurnFacts(
        principal=str(getattr(execution_context, "user_id", "") or ""),
        seat=str(seat or meta.get("owner_seat") or meta.get("seat") or "agent"),
        origin=_origin(execution_context, controller_self, taint_set),
        taints=taint_set,
        posture=_posture(),
        custody=_custody(),
        session_id=str(getattr(execution_context, "session_id", "") or ""),
        turn_id=str(meta.get("turn_id") or ""),
    )


__all__ = ["OWNER_SEAT_KIND", "SEATS", "ORIGINS", "TurnFacts", "owner_seat_context",
           "facts_of"]
