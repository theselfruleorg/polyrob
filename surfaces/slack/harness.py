"""Assemble the Slack surface: Web-API client + Socket Mode loop + routing.

Same shape as the Discord harness: dedup → parse → shared
``route_inbound``/``act_on_inbound`` pipeline → deliver back to the channel.
The shell is ``surfaces._shared.BaseHarness``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from core.surfaces.idempotency import IdempotencyStore
from surfaces._shared import BaseHarness, TextSink, register_surface_and_sink
from surfaces.slack.client import SlackClient
from surfaces.slack.socket_mode import SlackSocketModeClient, parse_event
from surfaces.slack.surface import SlackSurface

logger = logging.getLogger(__name__)


def SlackSink(client: SlackClient) -> TextSink:  # noqa: N802 — kept name
    """cron/delivery sink: send a raw text to a channel id (best-effort)."""
    return TextSink(client.send_message, label="SlackSink")


class SlackHarness(BaseHarness):
    surface_id = "slack"

    def __init__(self, container: Any, task_agent: Any, client: SlackClient,
                 socket: SlackSocketModeClient, dedup: IdempotencyStore) -> None:
        super().__init__(container, task_agent, dedup)
        self._client = client
        self._socket = socket
        self.bot_user_id: str = ""

    async def handle_event(self, event: dict) -> None:
        inbound = parse_event(event, self.bot_user_id,
                              user_directory=self._user_directory)
        if inbound is None or self._is_duplicate(inbound):
            return
        await self._route(inbound)

    async def _deliver_to(self, target, text: str) -> None:
        await self._client.send_message(target, text)

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
    register_surface_and_sink(container, SlackSurface(client), sink_name="slack_sink",
                              send=client.send_message, sink_label="SlackSink")
    return SlackHarness(container, task_agent, client, socket, dedup)
