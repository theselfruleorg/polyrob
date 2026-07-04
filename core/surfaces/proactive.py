"""Gate proactive (agent-initiated) sends by the target surface's send-policy.

WhatsApp's 24h window means a cron/self-wake/correspondent message outside the
window must downgrade to a template or be suppressed — never silently dropped or
rejected by the platform.

Usage::

    from core.surfaces.proactive import resolve_proactive_send
    action, extra = await resolve_proactive_send(container, "whatsapp", chat_id, text)
    # action in {"send", "template", "suppress"}

Surfaces without a window (Telegram, email, twitter) have no ``can_send_now``
method (or return ALLOW), so they always resolve to ("send", None).
"""
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _find_surface(container, surface_id: str):
    """Look up a surface object from the container via known service names."""
    if container is None:
        return None
    get = getattr(container, "get_service", None)
    if get is None:
        return None
    # 1. webhook_surfaces: dict[str, Surface]
    ws = get("webhook_surfaces")
    if isinstance(ws, dict):
        s = ws.get(surface_id)
        if s is not None:
            return s
    # 2. message_router: has ._surfaces dict
    mr = get("message_router")
    if mr is not None:
        s = getattr(mr, "_surfaces", {}).get(surface_id)
        if s is not None:
            return s
    return None


async def resolve_proactive_send(
    container,
    surface_id: str,
    session_key: str,
    text: str,
) -> Tuple[str, Optional[dict]]:
    """Return (action, extra) for a proactive send to *surface_id*.

    action is one of:
      "send"      — proceed with a free-text message
      "template"  — only a pre-approved template may be sent; extra["name"] has it
      "suppress"  — no message may be sent right now

    Fail-open: any lookup or policy error returns ("send", None) so the send still
    happens rather than silently killing cron/self-wake delivery.
    """
    from core.surfaces.send_policy import SendDecision

    surface = _find_surface(container, surface_id)
    if surface is None or not hasattr(surface, "can_send_now"):
        return ("send", None)

    try:
        decision = surface.can_send_now(session_key)
    except Exception as exc:
        logger.debug("resolve_proactive_send: can_send_now raised (fail-open): %s", exc)
        return ("send", None)

    if decision == SendDecision.ALLOW:
        return ("send", None)
    if decision == SendDecision.TEMPLATE_ONLY:
        from agents.task.surface_config import SurfaceConfig
        return ("template", {"name": SurfaceConfig.whatsapp_template_name()})
    # SendDecision.DENY or any unknown value
    return ("suppress", None)
