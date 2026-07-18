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


def register_inbound_actor(fn: Callable[..., Awaitable[Optional[str]]]) -> None:
    """Install THE shared inbound dispatch (surfaces.telegram.harness.act_on_inbound)."""
    global _INBOUND_ACTOR
    _INBOUND_ACTOR = fn


async def act_on_inbound(task_agent: Any, result: InboundResult, **kwargs) -> Optional[str]:
    """Delegate to the registered actor. Fail LOUDLY when none is registered —
    a webhook surface handling messages without the dispatch wired is a
    misconfiguration, not a case to fail-open on."""
    if _INBOUND_ACTOR is None:
        raise RuntimeError(
            "no inbound actor registered — import surfaces.telegram.harness "
            "(it registers the shared dispatch at import) before handling inbound messages"
        )
    return await _INBOUND_ACTOR(task_agent, result, **kwargs)
