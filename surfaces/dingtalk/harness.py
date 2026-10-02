"""Assemble the DingTalk surface: OpenAPI client + Stream Mode + routing.

Same shape as the Feishu harness: dedup → parse → shared
``route_inbound``/``act_on_inbound`` pipeline → deliver back to the
conversation (its session webhook while it lives). The shell is
``surfaces._shared.BaseHarness``.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from core.surfaces.idempotency import IdempotencyStore
from surfaces._shared import BaseHarness, register_surface_and_sink
from surfaces.dingtalk.client import Conversation, DingTalkClient
from surfaces.dingtalk.events import (is_group, parse_message, sender_id,
                                      webhook_expiry_s)
from surfaces.dingtalk.stream import DingTalkStream
from surfaces.dingtalk.surface import DingTalkSurface

logger = logging.getLogger(__name__)


class DingTalkHarness(BaseHarness):
    surface_id = "dingtalk"

    def __init__(self, container: Any, task_agent: Any, client: DingTalkClient,
                 stream: DingTalkStream, dedup: IdempotencyStore) -> None:
        super().__init__(container, task_agent, dedup)
        self._client = client
        self._stream = stream

    def _remember(self, data: dict) -> None:
        chat = str(data.get("conversationId") or "")
        webhook = str(data.get("sessionWebhook") or "")
        if chat:
            self._client.conversations.remember(chat, Conversation(
                webhook=webhook, expires_at=webhook_expiry_s(data),
                is_group=is_group(data), user_id=sender_id(data)))

    async def handle_callback(self, data: dict) -> None:
        try:
            inbound = parse_message(data, user_directory=self._user_directory)
            if inbound is None or self._is_duplicate(inbound):
                return
            self._remember(data)
            await self._route(inbound)
        except Exception:
            logger.warning("dingtalk inbound handler failed", exc_info=True)

    async def _deliver_to(self, target, text: str) -> None:
        await self._client.send_text(target, text)

    async def run(self) -> None:
        # Prove the app credential BEFORE the stream opens; a refused secret
        # fails loudly instead of looping on reconnect.
        await self._client.access_token()
        logger.info("dingtalk: app credential accepted")
        await self._stream.run(self.handle_callback)

    async def stop(self) -> None:
        await self._stream.stop()
        await self._client.close()


def build_dingtalk_harness(container: Any, task_agent: Any, *, client_id: str,
                           client_secret: str, data_dir: str = "data") -> DingTalkHarness:
    client = DingTalkClient(client_id, client_secret)
    stream = DingTalkStream(client_id, client_secret)
    dedup = IdempotencyStore(os.path.join(data_dir, "dingtalk_dedup.db"))
    register_surface_and_sink(container, DingTalkSurface(client), sink_name="dingtalk_sink",
                              send=client.send_text, sink_label="DingTalkSink")
    return DingTalkHarness(container, task_agent, client, stream, dedup)
