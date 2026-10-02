"""Assemble the WhatsApp surface: client + inbound (webhook) + outbound surface + window.
No polling — inbound arrives via the mounted /webhooks/whatsapp route (Phase 3). Registers
the surface on the MessageRouter and the webhook_surfaces registry, plus a cron sink."""
import logging
import os

from core.surfaces.idempotency import IdempotencyStore
from surfaces._shared import register_surface_and_sink
from surfaces.whatsapp.client import WhatsAppClient
from surfaces.whatsapp.inbound import DEDUP_WINDOW_S, WhatsAppInbound
from surfaces.whatsapp.surface import WhatsAppSurface
from surfaces.whatsapp.window import WindowTracker

logger = logging.getLogger(__name__)


class WhatsAppHarness:
    def __init__(self, surface, inbound): self._surface = surface; self._inbound = inbound

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def build_whatsapp_harness(container, task_agent, *, data_dir: str = "data"):
    client = WhatsAppClient()
    window = WindowTracker(os.path.join(data_dir, "wa_window.db"))
    user_directory = container.get_service("user_directory")
    inbound = WhatsAppInbound(
        IdempotencyStore(os.path.join(data_dir, "wa_dedup.db"),
                         window_seconds=DEDUP_WINDOW_S),
        user_directory=user_directory, window=window,
        media_fetch=client.download_media,
        responder=client.send_text,   # voice-guard / DENIED / transcript echo actually reach the user
        mark_read=client.mark_read,    # ✓✓ read receipt on voice notes
    )
    surface = WhatsAppSurface(client)
    surface.attach_window(window)

    # 030 WS-B2 / OS12: the ONE seam — register_surface (contract, surface
    # registry, router) plus the shared TextSink for cron/delivery.
    register_surface_and_sink(container, surface, sink_name="whatsapp_sink",
                              send=client.send_text, sink_label="WhatsAppSink")

    registry = container.get_service("webhook_surfaces") or {}
    registry["whatsapp"] = inbound
    container.register_service("webhook_surfaces", registry)

    return WhatsAppHarness(surface, inbound)
