"""WhatsAppSurface: outbound only (inbound is WhatsAppInbound/WebhookSurface). Thin —
rendering+splitting from the base (render_outbound); window policy from window.py (Task 4.3)."""
import logging

from core.surfaces.surface import Surface
from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities

logger = logging.getLogger(__name__)


def _wa_to(session_key: str) -> str:
    # agent:main:whatsapp:dm:<phone>[:user...] -> <phone> (SSOT parser, 030 WS-B3)
    from core.surfaces.session_chat_registry import chat_id_from_session_key
    return chat_id_from_session_key(session_key)


class WhatsAppSurface(Surface):
    def __init__(self, client) -> None:
        super().__init__()
        self._client = client

    @property
    def surface_id(self) -> str:
        return "whatsapp"

    @property
    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            supports_streaming=False, supports_edit=False, supports_interactive_ask=True,
            is_multi_tenant=True, max_message_bytes=4096, markdown_flavor="none",
            service_window_secs=86400, requires_template_outside_window=True,
        )

    async def send(self, msg: OutboundMessage) -> SendResult:
        to = _wa_to(msg.session_key)
        last = None
        try:
            for chunk in self.render_outbound(msg.text or ""):
                if not chunk:
                    continue
                resp = await self._client.send_text(to, chunk)
                last = (resp.get("messages") or [{}])[0].get("id")
            return SendResult(success=True, surface_message_id=last)
        except Exception as e:
            logger.error("WhatsAppSurface.send to %s failed: %s", to, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    def attach_window(self, tracker) -> None:
        self._window = tracker

    def last_inbound_at(self, session_key):
        """The 24 h window tracker (064 F4: the base ``can_send_now`` reads it)."""
        wt = getattr(self, "_window", None)
        if wt is None:
            return False, None
        return True, wt.last_inbound(_wa_to(session_key))

    async def start(self, container) -> None: ...
    async def stop(self) -> None: ...
