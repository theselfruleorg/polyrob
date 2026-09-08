"""Assemble the Slack surface: Web-API client + Socket Mode loop + routing.

Same shape as the Discord harness: dedup → parse → shared
``route_inbound``/``act_on_inbound`` pipeline → deliver back to the channel.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from core.surfaces.idempotency import IdempotencyStore
from surfaces.slack.client import SlackClient
from surfaces.slack.socket_mode import SlackSocketModeClient, parse_event
from surfaces.slack.surface import SlackSurface

logger = logging.getLogger(__name__)


class SlackSink:
    """cron/delivery sink: send a raw text to a channel id (best-effort)."""

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    async def send_message(self, chat_id, text) -> bool:
        try:
            await self._client.send_message(str(chat_id), str(text))
            return True
        except Exception:
            logger.warning("SlackSink.send_message failed for %s", chat_id,
                           exc_info=True)
            return False


class SlackHarness:
    def __init__(self, container: Any, task_agent: Any, client: SlackClient,
                 socket: SlackSocketModeClient, dedup: IdempotencyStore) -> None:
        self._container = container
        self._task_agent = task_agent
        self._client = client
        self._socket = socket
        self._dedup = dedup
        self._user_directory = container.get_service("user_directory") \
            if container else None
        self.bot_user_id: str = ""

    async def handle_event(self, event: dict) -> None:
        inbound = parse_event(event, self.bot_user_id,
                              user_directory=self._user_directory)
        if inbound is None:
            return
        if inbound.idempotency_key and self._dedup.seen(
                f"slack:{inbound.idempotency_key}"):
            return
        await self._route(inbound)

    async def _route(self, inbound) -> None:
        from surfaces._shared import route_and_act

        channel = inbound.identity.source.chat_id

        async def _deliver(text: str) -> None:
            try:
                await self._client.send_message(channel, text)
            except Exception:
                logger.warning("slack deliver failed", exc_info=True)

        await route_and_act(self._container, self._task_agent, inbound, _deliver)

    async def run(self) -> None:
        try:
            auth = await self._client.auth_test()
            self.bot_user_id = str(auth.get("user_id") or "")
            logger.info("slack connected as %s", self.bot_user_id)
        except Exception as e:
            logger.error("slack auth.test failed: %s", e)
            raise
        await self._socket.run(self.handle_event)

    async def stop(self) -> None:
        await self._socket.stop()
        await self._client.close()


def build_slack_harness(container: Any, task_agent: Any, *,
                        bot_token: Optional[str] = None,
                        app_token: Optional[str] = None,
                        data_dir: str = "data") -> SlackHarness:
    client = SlackClient(bot_token, app_token)
    socket = SlackSocketModeClient(client.connections_open)
    dedup = IdempotencyStore(os.path.join(data_dir, "slack_dedup.db"))
    surface = SlackSurface(client)

    # 030 WS-B2: register_surface enforces the contract, joins the surface
    # registry (so surface_profile() reaches the prompt) AND subscribes to
    # the router — the old bare subscribe left the agent blind to the shape.
    if container is not None:
        from core.surfaces.registry import register_surface
        register_surface(container, surface)
    if container is not None and container.get_service("slack_sink") is None:
        container.register_service("slack_sink", SlackSink(client))

    return SlackHarness(container, task_agent, client, socket, dedup)
