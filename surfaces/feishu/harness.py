"""Assemble the Feishu surface: Open-API client + WS long connection + routing.

Same shape as the Slack harness: dedup → parse → shared
``route_inbound``/``act_on_inbound`` pipeline → deliver back to the chat. The
shell is ``surfaces._shared.BaseHarness``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from core.surfaces.idempotency import IdempotencyStore
from surfaces._shared import BaseHarness, register_surface_and_sink
from surfaces.feishu.cards import EVENT_TYPE as CARD_EVENT, parse_card_action
from surfaces.feishu.client import FeishuClient
from surfaces.feishu.events import parse_event
from surfaces.feishu.surface import FeishuSurface
from surfaces.feishu.ws import FeishuLongConnection

logger = logging.getLogger(__name__)


#: Bound on the remembered chat types (a long-lived gateway sees many chats).
_CHAT_TYPES_MAX = 4096


def parse_inbound(payload: dict, bot_open_id: str, user_directory: Any,
                  chat_types: dict):
    """A message event or a card press → InboundMessage (or None). Shared by
    the WS harness and the webhook fallback."""
    header = payload.get("header") if isinstance(payload, dict) else None
    if isinstance(header, dict) and header.get("event_type") == CARD_EVENT:
        return parse_card_action(payload, user_directory=user_directory,
                                 chat_types=chat_types)
    inbound = parse_event(payload, bot_open_id, user_directory=user_directory)
    if inbound is not None:
        src = inbound.identity.source
        if len(chat_types) >= _CHAT_TYPES_MAX:
            chat_types.clear()
        chat_types[src.chat_id] = src.chat_type
    return inbound


class FeishuHarness(BaseHarness):
    surface_id = "feishu"

    def __init__(self, container: Any, task_agent: Any, client: FeishuClient,
                 socket: FeishuLongConnection, dedup: IdempotencyStore) -> None:
        super().__init__(container, task_agent, dedup)
        self._client = client
        self._socket = socket
        self.bot_open_id: str = ""
        #: chat_id → "dm" | "group", learned from inbound messages; a card
        #: callback does not carry it (unknown = group, the narrower tier).
        self.chat_types: dict = {}

    async def handle_event(self, payload: dict) -> None:
        try:
            inbound = parse_inbound(payload, self.bot_open_id, self._user_directory,
                                    self.chat_types)
            if inbound is None or self._is_duplicate(inbound):
                return
            await self._route(inbound)
        except Exception:
            logger.warning("feishu inbound handler failed", exc_info=True)

    async def _deliver_to(self, target, text: str) -> None:
        await self._client.send_message(target, text)

    async def _fetch_media(self, media) -> Optional[bytes]:
        """Order 0004: an inbound attachment's bytes, through the message-resource
        API. The tenant token rides ONLY to the configured Open-API host
        (``fetch_capped`` pins it); the size cap binds while reading."""
        ref = str(getattr(media, "ref", "") or "")
        message_id, _, file_key = ref.partition(":")
        if not (message_id and file_key):
            return None
        from surfaces._shared import fetch_capped
        return await fetch_capped(
            self._client.resource_url(message_id, file_key, getattr(media, "kind", "")),
            allowed_hosts=(self._client.host,), headers=await self._client.auth_header())

    async def run(self) -> None:
        # Prove the app credential and learn the bot's own open_id (the group
        # @-mention gate) BEFORE the socket opens; a refused secret fails loudly.
        info = await self._client.bot_info()
        self.bot_open_id = str(info.get("open_id") or "")
        if not self.bot_open_id:
            logger.warning("feishu: bot/v3/info returned no open_id — group "
                           "messages will be ignored (is the bot capability enabled?)")
        logger.info("feishu connected as %s", info.get("app_name") or "?")
        await self._socket.run(self.handle_event)

    async def stop(self) -> None:
        await self._socket.stop()
        await self._client.close()


def build_feishu_harness(container: Any, task_agent: Any, *, app_id: str,
                         app_secret: str, domain: Optional[str] = None,
                         data_dir: str = "data") -> FeishuHarness:
    client = FeishuClient(app_id, app_secret, domain=domain or "lark")
    socket = FeishuLongConnection(app_id, app_secret, domain=client.base)
    dedup = IdempotencyStore(os.path.join(data_dir, "feishu_dedup.db"))
    register_surface_and_sink(container, FeishuSurface(client), sink_name="feishu_sink",
                              send=client.send_message, sink_label="FeishuSink")
    return FeishuHarness(container, task_agent, client, socket, dedup)


async def build_feishu_webhook(container: Any, task_agent: Any, *, app_id: str,
                               app_secret: str, domain: Optional[str] = None,
                               encrypt_key: str = "", verification_token: str = "",
                               data_dir: str = "data", allow_unsigned: bool = False):
    """The webhook fallback (order 0003): registers ``webhook_surfaces['feishu']``
    — the gateway's shared ``/webhooks/<id>`` server carries the HTTP side.
    Returns ``(webhook, client)``; the caller closes the client on stop."""
    from surfaces.feishu.webhook import FeishuWebhook

    client = FeishuClient(app_id, app_secret, domain=domain or "lark")
    bot_open_id = ""
    try:
        bot_open_id = str((await client.bot_info()).get("open_id") or "")
    except Exception as e:
        logger.warning("feishu webhook: bot/v3/info failed (%s) — group messages "
                       "will be ignored until a restart", type(e).__name__)
    user_directory = container.get_service("user_directory") if container else None
    from surfaces.feishu.webhook import DEDUP_WINDOW_S
    hook = FeishuWebhook(IdempotencyStore(os.path.join(data_dir, "feishu_dedup.db"),
                                          window_seconds=DEDUP_WINDOW_S),
                         encrypt_key=encrypt_key, verification_token=verification_token,
                         bot_open_id=bot_open_id, user_directory=user_directory,
                         send=client.send_message, allow_unsigned=allow_unsigned)
    register_surface_and_sink(container, FeishuSurface(client), sink_name="feishu_sink",
                              send=client.send_message, sink_label="FeishuSink")
    if container is not None:
        registry = container.get_service("webhook_surfaces") or {}
        registry["feishu"] = hook
        container.register_service("webhook_surfaces", registry)
    return hook, client
