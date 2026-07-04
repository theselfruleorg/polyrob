"""Outbound-collapse mirror helpers (P1a).

Small factories that mirror an agent's discrete user-facing message into the
unified MessageRouter seam as a committed (partial=False) OutboundMessage. Additive
and gated on SINGULAR_CHAT_ENABLED; a no-op (and fail-open) when the flag is OFF or
no router / session_key has been bound, so the legacy add_to_feed path stays
byte-identical until later phases wire a router.
"""
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def build_discrete_publish(
    router: Any, session_key: Optional[str]
) -> Callable[..., Awaitable[None]]:
    async def _publish(text: str) -> None:
        from agents.task.surface_config import SurfaceConfig

        if not SurfaceConfig.singular_chat_enabled():
            return
        if router is None or not session_key:
            return
        try:
            from core.surfaces.envelopes import OutboundMessage, MessageKind

            await router.publish(OutboundMessage(
                session_key=session_key,
                text=text,
                kind=MessageKind.AGENT_TEXT,
                partial=False,
            ))
        except Exception as e:  # fail-open: outbound mirror is non-critical
            logger.debug("discrete publish mirror failed: %s", e)

    return _publish
