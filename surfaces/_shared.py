"""Shared polled-inbound tail (030 WS-B3/E2).

The four polling harnesses (discord, slack, signal, x) carried byte-similar
``_route`` bodies: route the inbound, build a deliver closure, run the shared
executor, deliver the immediate reply. ONE tail here; each harness keeps only
its transport-specific ``deliver`` (and optional typing) closure.

Lives in the surfaces tier (not core/) because importing
``surfaces.telegram.harness`` is what registers the shared inbound actor —
core must never import surfaces (layering ratchet).
"""
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


async def route_and_act(container: Any, task_agent: Any, inbound: Any,
                        deliver: Callable, *, spawn: Optional[Callable] = None) -> None:
    """route_inbound → act_on_inbound(deliver=) → deliver the immediate reply."""
    from core.surfaces.dispatcher import route_inbound
    from surfaces.telegram.harness import act_on_inbound  # registers the actor
    from surfaces.telegram.inbound import InboundResult

    decision = await route_inbound(container, inbound)
    reply = await act_on_inbound(
        task_agent, InboundResult(inbound=inbound, decision=decision),
        deliver=deliver, spawn=spawn)
    if reply:
        await deliver(reply)
