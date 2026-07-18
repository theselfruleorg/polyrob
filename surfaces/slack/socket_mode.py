"""Slack Socket Mode consumer + pure event→InboundMessage conversion.

Socket Mode: ``apps.connections.open`` yields a WS URL; every ``events_api``
envelope MUST be acknowledged (``{"envelope_id": ...}``) or Slack redelivers;
a ``disconnect`` message means reconnect (Slack rotates connections). No
public URL is needed — ideal for the local/daemon posture.

``parse_event`` is pure (unit-tested without a socket).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from core.surfaces.envelopes import Identity, InboundMessage, SessionSource

logger = logging.getLogger(__name__)


def parse_event(event: dict, bot_user_id: str,
                user_directory: Any = None) -> Optional[InboundMessage]:
    """Slack ``message`` event → InboundMessage, or None to ignore.

    Ignores: non-message events, our own/bot messages, and message subtypes
    (edits/joins/etc). ``channel_type == "im"`` → dm; anything else → group
    (W3 gating applies). ``thread_ts`` rides as thread_id.
    """
    if event.get("type") != "message":
        return None
    if event.get("subtype") or event.get("bot_id"):
        return None
    author_id = str(event.get("user") or "")
    if not author_id or author_id == str(bot_user_id):
        return None
    text = str(event.get("text") or "").strip()
    if not text:
        return None

    channel = str(event.get("channel") or "")
    is_dm = event.get("channel_type") == "im"
    source = SessionSource(surface_id="slack", chat_id=channel,
                           chat_type="dm" if is_dm else "group",
                           thread_id=str(event.get("thread_ts") or "") or None)

    user_id = None
    try:
        from core.instance import owner_surface_alias
        user_id = owner_surface_alias(author_id, "slack")
    except Exception:
        user_id = None
    if not user_id and user_directory is not None:
        try:
            user_id = user_directory.resolve_internal(author_id, "slack")
        except Exception:
            user_id = None
    if not user_id:
        user_id = f"u_slack_{author_id}"

    return InboundMessage(
        text=text,
        identity=Identity(user_id=user_id, source=source,
                          raw_user_id=author_id),
        idempotency_key=str(event.get("client_msg_id")
                            or f"{channel}:{event.get('ts')}"),
        raw=event,
        mentions_bot=f"<@{bot_user_id}>" in text,
    )


class SlackSocketModeClient:
    """Connect-and-dispatch loop over Socket Mode envelopes."""

    def __init__(self, connections_open) -> None:
        self._connections_open = connections_open
        self._stopped = asyncio.Event()
        self._tasks: set = set()

    async def stop(self) -> None:
        self._stopped.set()

    async def run(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        import aiohttp
        backoff = 1.0
        while not self._stopped.is_set():
            try:
                url = await self._connections_open()
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url) as ws:
                        backoff = 1.0
                        await self._consume(ws, handler)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("slack socket-mode error: %s — reconnecting in "
                               "%.0fs", e, backoff)
            if self._stopped.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _run_handler(self, handler, event: dict) -> None:
        try:
            await handler(event)
        except Exception:
            logger.warning("slack inbound handler failed", exc_info=True)

    async def _consume(self, ws, handler) -> None:
        import aiohttp
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            try:
                payload = json.loads(msg.data)
            except (json.JSONDecodeError, TypeError, ValueError):
                # One malformed frame must not tear down (and reconnect) the
                # whole socket — skip it.
                logger.warning("slack socket-mode: malformed frame skipped")
                continue
            kind = payload.get("type")
            envelope_id = payload.get("envelope_id")
            if envelope_id:
                # ACK FIRST — Slack redelivers unacked envelopes and a slow
                # agent turn must not look like a dead connection.
                await ws.send_json({"envelope_id": envelope_id})
            if kind == "disconnect":
                logger.info("slack socket-mode disconnect (%s) — rotating",
                            (payload.get("reason") or ""))
                return
            if kind == "events_api":
                event = ((payload.get("payload") or {}).get("event")) or {}
                # Dispatch WITHOUT awaiting: a multi-second agent turn awaited
                # inline blocks this read loop, so the NEXT envelope's ACK
                # slips past Slack's ~3s redelivery timer (redeliveries, and
                # eventually a dropped socket). Dedup in the harness absorbs
                # any duplicate delivery.
                task = asyncio.create_task(self._run_handler(handler, event))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            if self._stopped.is_set():
                return
