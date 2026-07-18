"""Discord Gateway (WS) client + pure event→InboundMessage conversion.

Hand-rolled minimal gateway consumer: HELLO → IDENTIFY → heartbeat loop →
MESSAGE_CREATE dispatch. On any disconnect it re-IDENTIFYs after an
exponential backoff (RESUME is deliberately not implemented in v1 — a
re-IDENTIFY loses at most the messages sent during the gap, and dedup +
session bindings make redelivery safe).

Intents: GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT.
``parse_message_create`` is pure (unit-tested without a socket).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from core.surfaces.envelopes import Identity, InboundMessage, SessionSource

logger = logging.getLogger(__name__)

INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)  # 37377

_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

# Discord docs: after INVALID_SESSION wait 1-5s before re-IDENTIFY, or the
# IDENTIFY rate limit locks the bot out. Module-level so tests can shrink it.
_INVALID_SESSION_DELAY_SEC = 2.5


def _mentions_bot(d: dict, bot_user_id: str) -> bool:
    if any(str(m.get("id")) == str(bot_user_id)
           for m in (d.get("mentions") or []) if isinstance(m, dict)):
        return True
    content = str(d.get("content") or "")
    return f"<@{bot_user_id}>" in content or f"<@!{bot_user_id}>" in content


def parse_message_create(d: dict, bot_user_id: str,
                         user_directory: Any = None) -> Optional[InboundMessage]:
    """MESSAGE_CREATE payload → InboundMessage, or None to ignore.

    Ignores: own messages, other bots, empty content. ``guild_id`` present →
    chat_type "group" (mention-gated by the dispatcher); else DM.
    """
    author = d.get("author") or {}
    author_id = str(author.get("id") or "")
    if not author_id or author_id == str(bot_user_id) or author.get("bot"):
        return None
    text = str(d.get("content") or "").strip()
    if not text:
        return None

    channel_id = str(d.get("channel_id") or "")
    is_group = bool(d.get("guild_id"))
    source = SessionSource(surface_id="discord", chat_id=channel_id,
                           chat_type="group" if is_group else "dm")

    user_id = None
    try:
        from core.instance import owner_surface_alias
        user_id = owner_surface_alias(author_id, "discord")
    except Exception:
        user_id = None
    if not user_id and user_directory is not None:
        try:
            user_id = user_directory.resolve_internal(author_id, "discord")
        except Exception:
            user_id = None
    if not user_id:
        user_id = f"u_discord_{author_id}"

    return InboundMessage(
        text=text,
        identity=Identity(user_id=user_id, source=source,
                          raw_user_id=author_id,
                          display_name=author.get("username")),
        idempotency_key=str(d.get("id") or "") or None,
        reply_to=str((d.get("message_reference") or {}).get("message_id") or "")
        or None,
        raw=d,
        mentions_bot=_mentions_bot(d, bot_user_id),
    )


class DiscordGatewayClient:
    """Connect-and-dispatch loop. ``handler`` receives raw MESSAGE_CREATE
    payload dicts; READY captures the bot user id (``self.bot_user_id``)."""

    def __init__(self, token: str, get_gateway_url) -> None:
        self._token = token
        self._get_gateway_url = get_gateway_url
        self.bot_user_id: Optional[str] = None
        self._stopped = asyncio.Event()

    async def stop(self) -> None:
        self._stopped.set()

    async def run(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        import aiohttp
        backoff = 1.0
        while not self._stopped.is_set():
            try:
                url = await self._get_gateway_url()
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                            f"{url}?v=10&encoding=json", heartbeat=None,
                            max_msg_size=8 * 1024 * 1024) as ws:
                        backoff = 1.0
                        await self._consume(ws, handler)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("discord gateway error: %s — reconnecting in %.0fs",
                               e, backoff)
            if self._stopped.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _consume(self, ws, handler) -> None:
        import aiohttp
        seq: Optional[int] = None
        heartbeat_task: Optional[asyncio.Task] = None
        acked = {"v": True}  # HEARTBEAT_ACK bookkeeping (half-open detection)
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                payload = json.loads(msg.data)
                op = payload.get("op")
                if payload.get("s") is not None:
                    seq = payload["s"]
                if op == _OP_HELLO:
                    interval = (payload["d"]["heartbeat_interval"]) / 1000.0

                    async def _beat():
                        while True:
                            await asyncio.sleep(interval)
                            if not acked["v"]:
                                # No ACK since our last beat: the socket is
                                # half-open (Discord will already have zombied
                                # it). Force-close so the read loop ends and
                                # the outer run() reconnects.
                                logger.warning("discord gateway: missed "
                                               "HEARTBEAT_ACK — reconnecting")
                                await ws.close()
                                return
                            acked["v"] = False
                            await ws.send_json({"op": _OP_HEARTBEAT, "d": seq})

                    heartbeat_task = asyncio.ensure_future(_beat())
                    await ws.send_json({
                        "op": _OP_IDENTIFY,
                        "d": {
                            "token": self._token,
                            "intents": INTENTS,
                            "properties": {"os": "linux", "browser": "polyrob",
                                           "device": "polyrob"},
                        },
                    })
                elif op == _OP_HEARTBEAT:
                    # Server-requested immediate beat (rare but mandatory).
                    await ws.send_json({"op": _OP_HEARTBEAT, "d": seq})
                elif op == _OP_HEARTBEAT_ACK:
                    acked["v"] = True
                elif op == _OP_RECONNECT:
                    logger.info("discord gateway: server RECONNECT — rotating")
                    return
                elif op == _OP_INVALID_SESSION:
                    logger.warning("discord gateway: INVALID_SESSION — waiting "
                                   "%.1fs before re-IDENTIFY",
                                   _INVALID_SESSION_DELAY_SEC)
                    await asyncio.sleep(_INVALID_SESSION_DELAY_SEC)
                    return
                elif op == _OP_DISPATCH:
                    event_type = payload.get("t")
                    d = payload.get("d") or {}
                    if event_type == "READY":
                        self.bot_user_id = str((d.get("user") or {}).get("id") or "")
                        logger.info("discord gateway READY as %s", self.bot_user_id)
                    elif event_type == "MESSAGE_CREATE":
                        try:
                            await handler(d)
                        except Exception:
                            logger.warning("discord inbound handler failed",
                                           exc_info=True)
                if self._stopped.is_set():
                    break
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
