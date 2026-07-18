"""Assemble the X DM surface: tweepy client + dm_events poller + routing.

Mirrors the Discord harness shape: every polled MessageCreate is parsed into an
InboundMessage, deduped (dm_event id), routed via the shared ``route_inbound``
→ ``act_on_inbound`` pipeline, and replies are delivered back to the DM
participant.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from core.surfaces.idempotency import IdempotencyStore
from surfaces.x.client import XDMClient
from surfaces.x.poller import XCursorStore, XDMPoller, parse_dm_event
from surfaces.x.surface import XSurface

logger = logging.getLogger(__name__)


class XSink:
    """cron/delivery sink: send a raw text as a DM to a participant id."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def send_message(self, chat_id, text) -> bool:
        try:
            await self._client.send_dm(str(chat_id), str(text))
            return True
        except Exception:
            logger.warning("XSink.send_message failed for %s", chat_id,
                           exc_info=True)
            return False


class XHarness:
    def __init__(self, container: Any, task_agent: Any, client: Any,
                 dedup: IdempotencyStore, *,
                 bot_user_id: Optional[str] = None,
                 poller: Optional[XDMPoller] = None) -> None:
        self._container = container
        self._task_agent = task_agent
        self._client = client
        self._dedup = dedup
        self._bot_user_id = bot_user_id
        self._poller = poller
        self._user_directory = container.get_service("user_directory") \
            if container else None

    async def handle_event(self, event: dict) -> None:
        inbound = parse_dm_event(event, self._bot_user_id or "",
                                 user_directory=self._user_directory)
        if inbound is None:
            return
        if inbound.idempotency_key and self._dedup.seen(
                f"x:{inbound.idempotency_key}"):
            return
        await self._route(inbound)

    async def _route(self, inbound) -> None:
        from core.surfaces.dispatcher import route_inbound
        from surfaces.telegram.harness import act_on_inbound
        from surfaces.telegram.inbound import InboundResult

        decision = await route_inbound(self._container, inbound)
        participant_id = inbound.identity.source.chat_id

        async def _deliver(text: str) -> None:
            try:
                await self._client.send_dm(participant_id, text)
            except Exception:
                logger.warning("x deliver failed", exc_info=True)

        reply = await act_on_inbound(
            self._task_agent,
            InboundResult(inbound=inbound, decision=decision),
            deliver=_deliver,
        )
        if reply:
            await _deliver(reply)

    async def run(self) -> None:
        if not self._bot_user_id:
            self._bot_user_id = (os.getenv("TWITTER_BOT_USER_ID") or "").strip() \
                or await self._client.get_me()
            logger.info("x dm surface online as user id %s", self._bot_user_id)
        if self._poller is None:
            raise RuntimeError("XHarness.run() needs a poller (build_x_harness)")
        await self._poller.run()

    async def stop(self) -> None:
        if self._poller is not None:
            await self._poller.stop()
        await self._client.close()


def build_x_harness(container: Any, task_agent: Any, *,
                    data_dir: str = "data",
                    client: Optional[Any] = None,
                    poll_sec: Optional[float] = None) -> XHarness:
    if client is None:
        creds = None
        cfg = getattr(container, "config", None)
        if cfg is not None and hasattr(cfg, "get_twitter_config"):
            try:
                creds = cfg.get_twitter_config()
            except Exception:
                creds = None
        client = XDMClient(creds)
    dedup = IdempotencyStore(os.path.join(data_dir, "x_dedup.db"))
    cursor = XCursorStore(os.path.join(data_dir, "x_cursor.json"))
    if poll_sec is None:
        try:
            poll_sec = float(os.getenv("X_DM_POLL_SEC", "90"))
        except ValueError:
            poll_sec = 90.0
    surface = XSurface(client)

    harness = XHarness(container, task_agent, client, dedup)
    harness._poller = XDMPoller(client, harness.handle_event, cursor,
                                poll_sec=poll_sec)

    router = container.get_service("message_router") if container else None
    if router is not None:
        router.subscribe("x", surface)
    if container is not None and container.get_service("x_sink") is None:
        container.register_service("x_sink", XSink(client))

    return harness
