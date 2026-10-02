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

#: Close codes after which a reconnect can never succeed (Discord docs,
#: "Gateway Close Event Codes"): re-IDENTIFYing on them in a loop makes
#: Discord reset the bot token. The gateway stops and says why instead.
FATAL_CLOSE_CODES = {
    4004: "Authentication failed — the bot token is wrong or was reset",
    4010: "Invalid shard",
    4011: "Sharding required",
    4012: "Invalid API version",
    4013: "Invalid intent(s)",
    4014: ("Disallowed intent(s) — enable the MESSAGE CONTENT privileged "
           "intent for this bot in the Discord Developer Portal"),
}


class DiscordGatewayFatal(RuntimeError):
    """The gateway closed with a code a reconnect cannot fix."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"discord gateway closed with {code}: {reason}")
        self.code = code
        self.reason = reason


def _mentions_bot(d: dict, bot_user_id: str) -> bool:
    if any(str(m.get("id")) == str(bot_user_id)
           for m in (d.get("mentions") or []) if isinstance(m, dict)):
        return True
    content = str(d.get("content") or "")
    return f"<@{bot_user_id}>" in content or f"<@!{bot_user_id}>" in content


#: Hosts a Discord attachment URL may point at (064 F3).
ATTACHMENT_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")


def parse_attachments(d: dict) -> list:
    """MESSAGE_CREATE ``attachments`` → ``Media`` (064 F3). The URL is kept as
    ``url``; the bytes are fetched only if the tier absorbs them."""
    from core.surfaces.media import Media, kind_for_mime
    out = []
    for a in d.get("attachments") or []:
        if not isinstance(a, dict) or not a.get("url"):
            continue
        mime = a.get("content_type") or None
        out.append(Media(kind=kind_for_mime(mime), mime=mime, url=str(a["url"]),
                         filename=a.get("filename") or None,
                         ref=str(a.get("id") or "") or None))
    return out


def parse_message_create(d: dict, bot_user_id: str,
                         user_directory: Any = None) -> Optional[InboundMessage]:
    """MESSAGE_CREATE payload → InboundMessage, or None to ignore.

    Ignores: own messages, other bots, a message with neither content nor an
    attachment (064 F3: a photo with no caption is a message). ``guild_id``
    present → chat_type "group" (mention-gated by the dispatcher); else DM.
    """
    author = d.get("author") or {}
    author_id = str(author.get("id") or "")
    if not author_id or author_id == str(bot_user_id) or author.get("bot"):
        return None
    text = str(d.get("content") or "").strip()
    media = parse_attachments(d)
    if not text and not media:
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
        media=media,
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
        #: OS3: in-flight MESSAGE_CREATE turns, held so the loop cannot GC them.
        self._handler_tasks: set = set()

    async def stop(self) -> None:
        self._stopped.set()

    async def run(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        """Connect, consume, reconnect. The backoff resets only after a READY
        (a socket that opens and then closes at once must not reconnect every
        second). A fatal close code stops the loop and raises."""
        backoff = 1.0
        while not self._stopped.is_set():
            ready = False
            try:
                ready = await self._connect_once(handler)
            except asyncio.CancelledError:
                raise
            except DiscordGatewayFatal as e:
                logger.error("discord gateway STOPPED: %s. The surface will not "
                             "reconnect until the gateway restarts.", e)
                self._stopped.set()
                raise
            except Exception as e:
                logger.warning("discord gateway error: %s — reconnecting in %.0fs",
                               e, backoff)
            if self._stopped.is_set():
                break
            if ready:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _connect_once(self, handler) -> bool:
        """One socket lifetime; True when READY arrived on it."""
        import aiohttp
        url = await self._get_gateway_url()
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                    f"{url}?v=10&encoding=json", heartbeat=None,
                    max_msg_size=8 * 1024 * 1024) as ws:
                return await self._consume(ws, handler)

    @staticmethod
    async def _run_handler(handler, d: dict) -> None:
        try:
            await handler(d)
        except Exception:
            logger.warning("discord inbound handler failed", exc_info=True)

    async def _consume(self, ws, handler) -> bool:
        """Read one socket until it ends. Returns True when READY arrived;
        raises :class:`DiscordGatewayFatal` on a fatal close code."""
        import aiohttp
        ready = False
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
                    return ready
                elif op == _OP_INVALID_SESSION:
                    logger.warning("discord gateway: INVALID_SESSION — waiting "
                                   "%.1fs before re-IDENTIFY",
                                   _INVALID_SESSION_DELAY_SEC)
                    await asyncio.sleep(_INVALID_SESSION_DELAY_SEC)
                    return ready
                elif op == _OP_DISPATCH:
                    event_type = payload.get("t")
                    d = payload.get("d") or {}
                    if event_type == "READY":
                        self.bot_user_id = str((d.get("user") or {}).get("id") or "")
                        ready = True
                        logger.info("discord gateway READY as %s", self.bot_user_id)
                    elif event_type == "MESSAGE_CREATE":
                        # OS3: a turn can take minutes. Awaited inline it
                        # stopped this read loop, the HEARTBEAT_ACK went
                        # unread, the watchdog closed the socket and the
                        # messages of the gap were lost. Per-chat order is
                        # kept by act_on_inbound's per-session lock.
                        from core.async_bridge import spawn_retained
                        spawn_retained(self._run_handler(handler, d),
                                       self._handler_tasks)
                if self._stopped.is_set():
                    break
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
        code = getattr(ws, "close_code", None)
        if isinstance(code, int) and code in FATAL_CLOSE_CODES:
            raise DiscordGatewayFatal(code, FATAL_CLOSE_CODES[code])
        return ready
