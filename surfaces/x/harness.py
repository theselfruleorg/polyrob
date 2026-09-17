"""Assemble the X DM surface: tweepy client + dm_events poller + routing.

Mirrors the Discord harness shape: every polled MessageCreate is parsed into an
InboundMessage, deduped (dm_event id), routed via the shared ``route_inbound``
→ ``act_on_inbound`` pipeline, and replies are delivered back to the DM
participant.
The shell is ``surfaces._shared.BaseHarness``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from core.surfaces.idempotency import IdempotencyStore
from surfaces._shared import BaseHarness, TextSink, register_surface_and_sink
from surfaces.x.client import XDMClient
from surfaces.x.poller import XCursorStore, XDMPoller, parse_dm_event
from surfaces.x.surface import XSurface

logger = logging.getLogger(__name__)


def XSink(client: Any) -> TextSink:  # noqa: N802 — kept name
    """cron/delivery sink: send a raw text as a DM to a participant id."""
    return TextSink(client.send_dm, label="XSink")


class XHarness(BaseHarness):
    surface_id = "x"

    def __init__(self, container: Any, task_agent: Any, client: Any,
                 dedup: IdempotencyStore, *,
                 bot_user_id: Optional[str] = None,
                 poller: Optional[XDMPoller] = None) -> None:
        super().__init__(container, task_agent, dedup)
        self._client = client
        self._bot_user_id = bot_user_id
        self._poller = poller

    async def handle_event(self, event: dict) -> None:
        inbound = parse_dm_event(event, self._bot_user_id or "",
                                 user_directory=self._user_directory)
        if inbound is None or self._is_duplicate(inbound):
            return
        await self._route(inbound)

    async def _deliver_to(self, target, text: str) -> None:
        await self._client.send_dm(target, text)

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
    harness = XHarness(container, task_agent, client, dedup)
    harness._poller = XDMPoller(client, harness.handle_event, cursor,
                                poll_sec=poll_sec)
    register_surface_and_sink(container, XSurface(client), sink_name="x_sink",
                              send=client.send_dm, sink_label="XSink")
    return harness
