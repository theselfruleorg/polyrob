"""Assemble the Discord surface: REST client + Gateway WS loop + routing.

Mirrors the Telegram harness shape: every MESSAGE_CREATE is deduped, parsed
into an InboundMessage, routed via the shared ``route_inbound`` →
``act_on_inbound`` pipeline (which enforces pairing, group allowlist,
mention-gating and the participant-as-DATA rail), and replies are delivered
back to the originating channel.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from core.surfaces.idempotency import IdempotencyStore
from surfaces.discord.client import DiscordClient
from surfaces.discord.gateway import DiscordGatewayClient, parse_message_create
from surfaces.discord.surface import DiscordSurface

logger = logging.getLogger(__name__)


class DiscordSink:
    """cron/delivery sink: send a raw text to a channel id (best-effort)."""

    def __init__(self, client: DiscordClient) -> None:
        self._client = client

    async def send_message(self, chat_id, text) -> bool:
        try:
            await self._client.send_message(str(chat_id), str(text))
            return True
        except Exception:
            logger.warning("DiscordSink.send_message failed for %s", chat_id,
                           exc_info=True)
            return False


class DiscordHarness:
    def __init__(self, container: Any, task_agent: Any, client: DiscordClient,
                 gateway: DiscordGatewayClient, dedup: IdempotencyStore) -> None:
        self._container = container
        self._task_agent = task_agent
        self._client = client
        self._gateway = gateway
        self._dedup = dedup
        self._user_directory = container.get_service("user_directory") \
            if container else None

    async def handle_message_create(self, d: dict) -> None:
        bot_id = self._gateway.bot_user_id or ""
        inbound = parse_message_create(d, bot_id,
                                       user_directory=self._user_directory)
        if inbound is None:
            return
        if inbound.idempotency_key and self._dedup.seen(
                f"discord:{inbound.idempotency_key}"):
            return
        await self._route(inbound)

    async def _route(self, inbound) -> None:
        from core.surfaces.dispatcher import route_inbound
        from surfaces.telegram.harness import act_on_inbound
        from surfaces.telegram.inbound import InboundResult

        decision = await route_inbound(self._container, inbound)
        channel_id = inbound.identity.source.chat_id

        async def _deliver(text: str) -> None:
            try:
                await self._client.send_message(channel_id, text)
            except Exception:
                logger.warning("discord deliver failed", exc_info=True)

        try:
            await self._client.trigger_typing(channel_id)
        except Exception:
            pass
        reply = await act_on_inbound(
            self._task_agent,
            InboundResult(inbound=inbound, decision=decision),
            deliver=_deliver,
        )
        if reply:
            await _deliver(reply)

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
    surface = DiscordSurface(client)

    router = container.get_service("message_router") if container else None
    if router is not None:
        router.subscribe("discord", surface)
    if container is not None and container.get_service("discord_sink") is None:
        container.register_service("discord_sink", DiscordSink(client))

    return DiscordHarness(container, task_agent, client, gateway, dedup)
