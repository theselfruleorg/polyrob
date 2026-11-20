"""Inbound-act contract (R-4 inversion, 2026-07-17).

core previously imported ``surfaces.telegram.harness.act_on_inbound`` /
``surfaces.telegram.inbound.InboundResult`` at the top of ``inbound_webhook.py`` —
a core→surfaces upward edge. core now owns the envelope (``InboundResult``) and a
registration seam; ``surfaces/telegram/harness.py`` registers the shared
RouteDecision→TaskAgent dispatch at import time, and every concrete webhook
surface module imports the harness (directly or transitively) before handling
messages.
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from core.surfaces.dispatcher import RouteDecision
from core.surfaces.envelopes import InboundMessage


@dataclass
class InboundResult:
    inbound: InboundMessage
    decision: RouteDecision


_INBOUND_ACTOR: Optional[Callable[..., Awaitable[Optional[str]]]] = None
#: 126: the shared executor's owner-COMMAND entry (``harness._handle_command``).
_OWNER_COMMAND: Optional[Callable[..., Awaitable[Any]]] = None


def register_inbound_actor(fn: Callable[..., Awaitable[Optional[str]]], *,
                           command: Optional[Callable[..., Awaitable[Any]]] = None) -> None:
    """Install THE shared inbound dispatch (surfaces.telegram.harness.act_on_inbound)
    and, with ``command``, its owner-command entry (:func:`run_owner_line`)."""
    global _INBOUND_ACTOR, _OWNER_COMMAND
    _INBOUND_ACTOR = fn
    if command is not None:
        _OWNER_COMMAND = command


def owner_line_ready() -> bool:
    """True when this process registered the shared executor (:func:`run_owner_line`)."""
    return _OWNER_COMMAND is not None


async def run_owner_line(task_agent: Any, user_id: str, text: str) -> Any:
    """126: run ONE owner line through the shared executor exactly as the owner
    typing it in the owner's DM — every gate of that path runs. The tools tier
    uses it only for a card's tap token (a proposal's quote). None when no
    executor is registered in this process (the caller keeps its own fallback)."""
    if _OWNER_COMMAND is None:
        return None
    from core.surfaces.dispatcher import RouteKind
    from core.surfaces.envelopes import Identity, SessionSource
    uid = str(user_id)
    source = SessionSource(surface_id="telegram", chat_id=uid, chat_type="dm")
    inbound = InboundMessage(text=text, identity=Identity(user_id=uid, source=source))
    decision = RouteDecision(kind=RouteKind.COMMAND,
                             session_key=f"agent:main:telegram:dm:{uid}:{uid}",
                             command=(text or "").split()[0] if (text or "").strip() else "")
    return await _OWNER_COMMAND(task_agent, InboundResult(inbound=inbound, decision=decision),
                                None, None)


async def act_on_inbound(task_agent: Any, result: InboundResult, **kwargs) -> Optional[str]:
    """Delegate to the registered actor. Fail LOUDLY when none is registered —
    a webhook surface handling messages without the dispatch wired is a
    misconfiguration, not a case to fail-open on."""
    if _INBOUND_ACTOR is None:
        raise RuntimeError(
            "no inbound actor registered — import surfaces._actor "
            "(ensure_registered() wires the shared dispatch) before handling inbound messages"
        )
    return await _INBOUND_ACTOR(task_agent, result, **kwargs)
