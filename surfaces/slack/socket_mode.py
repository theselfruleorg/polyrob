"""Slack Socket Mode consumer + pure event→InboundMessage conversion.

Socket Mode: ``apps.connections.open`` yields a WS URL; every ``events_api``
envelope MUST be acknowledged (``{"envelope_id": ...}``) or Slack redelivers;
a ``disconnect`` message means reconnect (Slack rotates connections). No
public URL is needed — ideal for the local/daemon posture. A ``slash_commands``
envelope (the app's one command, 113 W7) is acked the same way and routed as
the verb line its text names.

``parse_event`` and ``parse_slash_command`` are pure (unit-tested without a socket).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from core.surfaces.envelopes import Identity, InboundMessage, SessionSource

logger = logging.getLogger(__name__)


#: Hosts a Slack file URL may point at (064 F3). The fetch sends the BOT
#: TOKEN, so nothing outside Slack's own file host may ever receive it.
FILE_HOSTS = ("files.slack.com",)


def parse_files(event: dict) -> list:
    """A message's ``files`` → ``Media`` (064 F3); bytes fetched later, by tier."""
    from core.surfaces.media import Media, kind_for_mime
    out = []
    for f in event.get("files") or []:
        if not isinstance(f, dict):
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        mime = f.get("mimetype") or None
        out.append(Media(kind=kind_for_mime(mime), mime=mime, url=str(url),
                         filename=f.get("name") or None,
                         ref=str(f.get("id") or "") or None))
    return out


def _internal_user_id(author_id: str, user_directory: Any = None) -> str:
    """The owner alias, else the user directory, else a namespaced fallback."""
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
    return user_id or f"u_slack_{author_id}"


#: The Socket Mode envelope type of a slash command, and the type the consumer
#: hands to the harness (the command body has no ``type`` of its own).
SLASH_ENVELOPE = "slash_commands"


def slash_line(text: str) -> str:
    """The verb line of an app command: ``/<app> status`` -> ``/status``.

    Slack's own client takes every line that starts with ``/`` as a Slack
    command, so a person cannot type ``/status`` to the bot. The app registers
    ONE command; its text is the verb and its words. ``/<app> /status`` is the
    same line, and ``/<app>`` alone is ``/help``."""
    words = str(text or "").strip()
    if not words:
        return "/help"
    return words if words.startswith("/") else "/" + words


def parse_slash_command(body: dict, user_directory: Any = None) -> Optional[InboundMessage]:
    """A ``slash_commands`` envelope payload -> InboundMessage, or None.

    The payload carries the same fields as Slack's HTTP slash-command request
    (``command``, ``text``, ``user_id``, ``channel_id``, ``channel_name``,
    ``trigger_id``). A command run in the app's DM (a ``D…`` channel, or
    ``channel_name == "directmessage"``) is a dm; anywhere else it is a room
    line that names the bot — the person addressed the app by its command."""
    if not isinstance(body, dict):
        return None
    author_id = str(body.get("user_id") or "")
    channel = str(body.get("channel_id") or "")
    if not author_id or not channel or not body.get("command"):
        return None
    is_dm = channel.startswith("D") or body.get("channel_name") == "directmessage"
    source = SessionSource(surface_id="slack", chat_id=channel,
                           chat_type="dm" if is_dm else "group")
    trigger = str(body.get("trigger_id") or "")
    return InboundMessage(
        text=slash_line(body.get("text") or ""),
        identity=Identity(user_id=_internal_user_id(author_id, user_directory),
                          source=source, raw_user_id=author_id,
                          display_name=str(body.get("user_name") or "") or None),
        idempotency_key=f"slash:{trigger}" if trigger else None,
        raw=body,
        mentions_bot=True,
    )


def parse_event(event: dict, bot_user_id: str,
                user_directory: Any = None) -> Optional[InboundMessage]:
    """Slack ``message`` event → InboundMessage, or None to ignore.

    Ignores: non-message events, our own/bot messages, and message subtypes
    (edits/joins/etc) — except ``file_share`` (064 F3: a message that carries
    files), which is read with its ``files``. ``channel_type == "im"`` → dm;
    anything else → group (W3 gating applies). ``thread_ts`` rides as thread_id.
    """
    if event.get("type") != "message":
        return None
    if event.get("subtype") not in (None, "file_share") or event.get("bot_id"):
        return None
    author_id = str(event.get("user") or "")
    if not author_id or author_id == str(bot_user_id):
        return None
    text = str(event.get("text") or "").strip()
    media = parse_files(event)
    if not text and not media:
        return None

    channel = str(event.get("channel") or "")
    is_dm = event.get("channel_type") == "im"
    source = SessionSource(surface_id="slack", chat_id=channel,
                           chat_type="dm" if is_dm else "group",
                           thread_id=str(event.get("thread_ts") or "") or None)
    user_id = _internal_user_id(author_id, user_directory)

    return InboundMessage(
        text=text,
        identity=Identity(user_id=user_id, source=source,
                          raw_user_id=author_id),
        idempotency_key=str(event.get("client_msg_id")
                            or f"{channel}:{event.get('ts')}"),
        raw=event,
        media=media,
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
            event = None
            if kind == "events_api":
                event = ((payload.get("payload") or {}).get("event")) or {}
            elif kind == SLASH_ENVELOPE:
                # 113 W7 F-01: the app's one command. Acked above with an empty
                # body (Slack shows nothing); the answer is a normal message.
                event = {"type": SLASH_ENVELOPE, "payload": payload.get("payload") or {}}
            if event is not None:
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
