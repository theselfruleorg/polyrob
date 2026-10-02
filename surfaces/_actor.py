"""The ONE import a surface makes to reach the shared inbound dispatch (064 F1).

A surface turns its platform event into an ``InboundMessage``, routes it
(``core.surfaces.dispatcher.route_inbound``) and hands the ``RouteDecision`` to
:func:`act_on_inbound` — the RouteDecision → TaskAgent executor every surface
shares (steer / new session / owner command / correspondent data).

Before 064 F1, email, WhatsApp and the four polled harnesses each imported
``surfaces.telegram.harness`` by name to reach it, so a new surface had to
know that the shared executor happened to live in the Telegram package. They
import THIS module now.

⚠️ The executor's body still lives in ``surfaces/telegram/harness.py`` — it
pulls in some twenty ``surfaces/telegram/*_ops`` modules (owner verbs, rooms,
groups, dev rail). Moving that body is a separate, mechanical change; this seam
means no surface has to change when it moves. The lookup is by ATTRIBUTE at
call time, so a test that patches ``surfaces.telegram.harness.act_on_inbound``
still intercepts every surface.
"""
from typing import Any, Optional

from core.surfaces.act import InboundResult  # noqa: F401 — the envelope, re-exported


def _home():
    import surfaces.telegram.harness as home  # registers the core actor at import
    return home


def ensure_registered() -> None:
    """Register the shared executor with ``core.surfaces.act`` (idempotent).
    A webhook surface calls this at import; core delegates through it."""
    _home()


async def act_on_inbound(task_agent: Any, result: InboundResult, **kwargs) -> Optional[str]:
    """Execute one routed inbound — the shared executor, whatever its home."""
    return await _home().act_on_inbound(task_agent, result, **kwargs)
