"""Assemble the WhatsApp surface: client + inbound (webhook) + outbound surface + window.
No polling — inbound arrives via the mounted /webhooks/whatsapp route (Phase 3). Registers
the surface on the MessageRouter and the webhook_surfaces registry, plus a cron sink."""
import logging
import os

from core.surfaces.idempotency import IdempotencyStore
from surfaces.whatsapp.client import WhatsAppClient
from surfaces.whatsapp.inbound import WhatsAppInbound
from surfaces.whatsapp.surface import WhatsAppSurface
from surfaces.whatsapp.window import WindowTracker

logger = logging.getLogger(__name__)


class WhatsAppSink:
    """cron/delivery sink: send a raw text to a wa phone (best-effort)."""
    def __init__(self, client): self._client = client

    async def send_message(self, chat_id, text) -> bool:
        try:
            await self._client.send_text(str(chat_id), text)
            return True
        except Exception:
            logger.warning("WhatsAppSink.send_message failed for %s", chat_id, exc_info=True)
            return False


class WhatsAppHarness:
    def __init__(self, surface, inbound): self._surface = surface; self._inbound = inbound

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def build_whatsapp_harness(container, task_agent, *, data_dir: str = "data"):
    client = WhatsAppClient()
    window = WindowTracker(os.path.join(data_dir, "wa_window.db"))
    user_directory = container.get_service("user_directory")
    inbound = WhatsAppInbound(
        IdempotencyStore(os.path.join(data_dir, "wa_dedup.db")),
        user_directory=user_directory, window=window,
        media_fetch=client.download_media,
        responder=client.send_text,   # voice-guard / DENIED / transcript echo actually reach the user
        mark_read=client.mark_read,    # ✓✓ read receipt on voice notes
    )
    surface = WhatsAppSurface(client)
    surface.attach_window(window)

    router = container.get_service("message_router")
    if router is not None:
        router.subscribe("whatsapp", surface)

    registry = container.get_service("webhook_surfaces") or {}
    registry["whatsapp"] = inbound
    container.register_service("webhook_surfaces", registry)

    if container.get_service("whatsapp_sink") is None:
        container.register_service("whatsapp_sink", WhatsAppSink(client))

    return WhatsAppHarness(surface, inbound)
