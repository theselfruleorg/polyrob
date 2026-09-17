"""Assemble the Discord surface: REST client + Gateway WS loop + routing.

Mirrors the Telegram harness shape: every MESSAGE_CREATE is deduped, parsed
into an InboundMessage, routed via the shared ``route_inbound`` →
``act_on_inbound`` pipeline (which enforces pairing, group allowlist,
mention-gating and the participant-as-DATA rail), and replies are delivered
back to the originating channel. The shell is ``surfaces._shared.BaseHarness``.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from core.surfaces.idempotency import IdempotencyStore
from surfaces._shared import BaseHarness, TextSink, register_surface_and_sink
from surfaces.discord.client import DiscordClient
from surfaces.discord.gateway import DiscordGatewayClient, parse_message_create
from surfaces.discord.surface import DiscordSurface


def DiscordSink(client: DiscordClient) -> TextSink:  # noqa: N802 — kept name
    """cron/delivery sink: send a raw text to a channel id (best-effort)."""
    return TextSink(client.send_message, label="DiscordSink")


class DiscordHarness(BaseHarness):
    surface_id = "discord"

    def __init__(self, container: Any, task_agent: Any, client: DiscordClient,
                 gateway: DiscordGatewayClient, dedup: IdempotencyStore) -> None:
        super().__init__(container, task_agent, dedup)
        self._client = client
        self._gateway = gateway

    async def handle_message_create(self, d: dict) -> None:
        bot_id = self._gateway.bot_user_id or ""
        inbound = parse_message_create(d, bot_id,
                                       user_directory=self._user_directory)
        if inbound is None or self._is_duplicate(inbound):
            return
        await self._route(inbound)

    async def _before_route(self, target) -> None:
        await self._client.trigger_typing(target)

    async def _deliver_to(self, target, text: str) -> None:
        await self._client.send_message(target, text)

    async def run(self) -> None:
        await self._gateway.run(self.handle_message_create)

    async def stop(self) -> None:
        await self._gateway.stop()
        await self._client.close()


def build_discord_harness(container: Any, task_agent: Any, *,
                          token: Optional[str] = None,
                          data_dir: str = "data") -> DiscordHarness:
    client = DiscordClient(token)
    gateway = DiscordGatewayClient(token or os.getenv("DISCORD_BOT_TOKEN", ""),
                                   client.get_gateway_url)
    dedup = IdempotencyStore(os.path.join(data_dir, "discord_dedup.db"))
    register_surface_and_sink(container, DiscordSurface(client), sink_name="discord_sink",
                              send=client.send_message, sink_label="DiscordSink")
    return DiscordHarness(container, task_agent, client, gateway, dedup)
