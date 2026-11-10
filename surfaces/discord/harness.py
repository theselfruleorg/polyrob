"""Assemble the Discord surface: REST client + Gateway WS loop + routing.

Mirrors the Telegram harness shape: every MESSAGE_CREATE is deduped, parsed
into an InboundMessage, routed via the shared ``route_inbound`` →
``act_on_inbound`` pipeline (which enforces pairing, group allowlist,
mention-gating and the participant-as-DATA rail), and replies are delivered
back to the originating channel. The shell is ``surfaces._shared.BaseHarness``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from core.surfaces.idempotency import IdempotencyStore
from surfaces._shared import (FAILED_MARK, SEEN_MARK, BaseHarness, TextSink,
                               register_surface_and_sink, wants_feedback)
from surfaces.discord.client import DiscordClient
from surfaces.discord.gateway import DiscordGatewayClient, parse_message_create
from surfaces.discord.surface import DiscordSurface

logger = logging.getLogger(__name__)

#: Seconds between typing renewals (Discord shows typing for about ten).
TYPING_PULSE_S = 8.0


def DiscordSink(client: DiscordClient) -> TextSink:  # noqa: N802 — kept name
    """cron/delivery sink: send a raw text to a channel id (best-effort)."""
    return TextSink(client.send_message, label="DiscordSink", surface_id="discord")


class DiscordHarness(BaseHarness):
    surface_id = "discord"

    def __init__(self, container: Any, task_agent: Any, client: DiscordClient,
                 gateway: DiscordGatewayClient, dedup: IdempotencyStore) -> None:
        super().__init__(container, task_agent, dedup)
        self._client = client
        self._gateway = gateway
        self._pulses: dict = {}   # id(inbound) -> the typing-pulse task

    async def handle_message_create(self, d: dict) -> None:
        bot_id = self._gateway.bot_user_id or ""
        inbound = parse_message_create(d, bot_id,
                                       user_directory=self._user_directory)
        if inbound is None or self._is_duplicate(inbound):
            return
        await self._route(inbound)

    async def _before_route(self, target) -> None:
        await self._client.trigger_typing(target)

    @staticmethod
    def _message_ref(inbound):
        """``(channel, message id)`` to react on, or None (see ``wants_feedback``)."""
        if not wants_feedback(inbound):
            return None
        raw = inbound.raw if isinstance(inbound.raw, dict) else {}
        mid = str(raw.get("id") or "")
        channel = str(inbound.identity.source.chat_id or "")
        return (channel, mid) if mid and channel else None

    async def _typing_pulse(self, channel: str) -> None:
        """Discord's typing state lasts about ten seconds; a turn takes longer.
        Renewed until the turn ends (cancelled in ``on_processing_finish``)."""
        while True:
            await asyncio.sleep(TYPING_PULSE_S)
            try:
                await self._client.trigger_typing(channel)
            except Exception:
                logger.debug("discord typing pulse failed", exc_info=True)

    async def on_processing_start(self, inbound) -> None:
        ref = self._message_ref(inbound)
        if not ref:
            return
        key = id(inbound)
        self._pulses[key] = asyncio.create_task(self._typing_pulse(ref[0]))
        await self._client.add_reaction(*ref, SEEN_MARK)

    async def on_processing_finish(self, inbound, ok: bool) -> None:
        pulse = self._pulses.pop(id(inbound), None)
        if pulse is not None:
            pulse.cancel()
        ref = self._message_ref(inbound)
        if not ref:
            return
        try:
            await self._client.remove_reaction(*ref, SEEN_MARK)
        finally:
            if not ok:
                await self._client.add_reaction(*ref, FAILED_MARK)

    async def _deliver_to(self, target, text: str) -> None:
        await self._client.send_message(target, text)

    async def _deliver_dm(self, address: str, text: str) -> None:
        """A user id is not a channel id on Discord: open (or fetch) the DM first."""
        await self._client.send_message(await self._client.create_dm(address), text)

    async def _fetch_media(self, media) -> Optional[bytes]:
        """A Discord CDN attachment (public signed URL — no token sent)."""
        from surfaces._shared import fetch_capped
        from surfaces.discord.gateway import ATTACHMENT_HOSTS
        return await fetch_capped(media.url, allowed_hosts=ATTACHMENT_HOSTS)

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
