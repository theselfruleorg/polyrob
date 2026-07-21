"""MessageRouter: the SINGLE outbound producer seam.

Every agent->user emit funnels through publish(). Text is brain-scrubbed here
(SSOT = modules.llm.brain_scrubber) so every surface inherits the scrub. Surfaces
subscribe by surface_id; routing resolves session_key -> surface_id via the
SessionChatRegistry. Fail-open: an unroutable key or a raising surface never
crashes the agent loop.
"""
import logging

from core.surfaces.envelopes import OutboundMessage
from core.surfaces.session_chat_registry import SessionChatRegistry
from modules.llm.brain_scrubber import scrub_brain_blocks

logger = logging.getLogger(__name__)


class MessageRouter:
    def __init__(self, registry: SessionChatRegistry) -> None:
        self._registry = registry
        self._surfaces: dict[str, object] = {}
        self._queue = None

    def attach_queue(self, q) -> None:
        """Attach a durable OutboundDeliveryQueue. Call from bootstrap after construction."""
        self._queue = q

    def subscribe(self, surface_id: str, surface) -> None:
        self._surfaces[surface_id] = surface

    def capabilities(self, surface_id: str):
        """Capabilities of a subscribed surface, or None if unknown (mirrors
        SurfaceRegistry.capabilities). Used by callers (e.g. the `message` action)
        that need to know ahead of a send whether a surface can render media."""
        surface = self._surfaces.get(surface_id)
        return getattr(surface, "capabilities", None) if surface is not None else None

    def bot_username(self, surface_id: str):
        """The subscribed surface's own bot handle (no ``@``), or None if unknown.
        Lets a caller recognize "the agent addressed its own handle" (e.g. an
        owner-notify `message` action that mistakenly used its own @username as
        the target) without hardcoding any deployment-specific username."""
        surface = self._surfaces.get(surface_id)
        return getattr(surface, "bot_username", None) if surface is not None else None

    async def publish(self, msg: OutboundMessage) -> None:
        try:
            scrubbed = scrub_brain_blocks(msg.text)
        except Exception:  # fail-open: never drop a reply over a scrub bug
            scrubbed = msg.text
        # F3 parity with HITLManager.stream_output: a wholly-brain (or empty) chunk
        # scrubs to None/"" and must be DROPPED, not delivered as an empty bubble.
        if scrubbed is None or not scrubbed.strip():
            return
        if scrubbed != msg.text:
            msg = OutboundMessage(
                session_key=msg.session_key, text=scrubbed, kind=msg.kind,
                partial=msg.partial, stream_id=msg.stream_id,
                reply_to=msg.reply_to, media=msg.media,
            )
        row = self._registry.resolve(msg.session_key)
        if not row:
            logger.debug("message_router: no binding for %s; dropping", msg.session_key)
            return
        surface = self._surfaces.get(row.get("surface_id"))
        if surface is None:
            logger.debug("message_router: no surface %s subscribed", row.get("surface_id"))
            return
        # Durable path (final messages only): enqueue instead of sending directly.
        from agents.task.surface_config import SurfaceConfig
        if (self._queue is not None and not msg.partial
                and SurfaceConfig.outbound_queue_enabled()):
            turn = msg.stream_id or msg.session_key
            idem = f"{msg.session_key}#{turn}#{hash(scrubbed) & 0xffffffff}"
            try:
                self._queue.enqueue(
                    idempotency_key=idem, session_key=msg.session_key,
                    surface_id=row.get("surface_id"), dest=row.get("chat_id"),
                    payload=scrubbed, kind=str(getattr(msg.kind, "value", msg.kind)),
                )
            except Exception as e:  # fail-open: fall back to a direct send on a queue fault
                logger.error("outbound enqueue failed, sending directly: %s", e)
            else:
                return
        try:
            if msg.partial:
                await surface.stream(msg)  # base buffers if surface can't stream
            else:
                await surface.send(msg)
        except Exception as e:  # fail-open
            logger.error("message_router: surface %s raised: %s", row.get("surface_id"), e, exc_info=True)

    async def send_message(self, chat_id: str, text: str, surface_id: str = "telegram",
                            media: list | None = None) -> bool:
        """Back-compat shim for cron/delivery.py + the `message` tool. Returns True only
        on a completed send. `media` defaults to None -> OutboundMessage(media=[]),
        keeping today's shape byte-identical when no media is given."""
        surface = self._surfaces.get(surface_id)
        if surface is None:
            logger.warning("send_message: no surface %s registered — delivery failed", surface_id)
            return False
        try:
            await surface.send(OutboundMessage(
                session_key=f"direct:{surface_id}:{chat_id}", text=text,
                media=media or [],
            ))
            return True
        except Exception as e:
            logger.error("send_message shim failed: %s", e, exc_info=True)
            return False
