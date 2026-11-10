"""Shared polled-inbound tail (030 WS-B3/E2).

The four polling harnesses (discord, slack, signal, x) carried byte-similar
``_route`` bodies: route the inbound, build a deliver closure, run the shared
executor, deliver the immediate reply. ONE tail here; each harness keeps only
its transport-specific ``deliver`` (and optional typing) closure.

Lives in the surfaces tier (not core/) because it reaches the shared inbound
executor through ``surfaces._actor`` — core must never import surfaces
(layering ratchet).
"""
import asyncio
import logging
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


def split_for_surface(surface_id: str, text: str) -> List[str]:
    """*text* in chunks that fit ``surface_id``'s per-message limit (OS1).

    The limit is the catalog's ``max_message_chars`` — the ONE number per
    platform. A reply over it (``/help`` is ~6,600 chars) used to go out whole:
    Discord answered 400, WhatsApp an error, and the user got nothing. An
    unknown surface gets one chunk.
    """
    text = text or ""
    try:
        from core.surfaces.catalog import get as _spec
        spec = _spec(surface_id)
        limit = int(spec.max_message_chars) if spec is not None else 0
    except Exception:
        limit = 0
    if limit <= 0 or len(text) <= limit:
        return [text]
    from core.surfaces.surface import split_message
    return [c for c in split_message(text, limit) if c] or [text]


async def route_and_act(container: Any, task_agent: Any, inbound: Any,
                        deliver: Callable, *, spawn: Optional[Callable] = None,
                        fetch_media: Optional[Callable] = None,
                        deliver_dm: Optional[Callable] = None) -> None:
    """route_inbound → act_on_inbound(deliver=, fetch_media=) → deliver the reply.

    ``fetch_media`` (064 F3) is the surface's async ``(Media) -> bytes | None``.
    The shared executor decides by TIER what happens to the files: the owner's
    are stored in the session workspace, a correspondent's are only named.

    ``deliver_dm`` is the surface's async ``(address, text)`` to a person's DM.
    With it, the owner's verb typed in a room is answered in his DM and the room
    reads one line that says so (113 W7 F-03); without it that reply is dropped.
    """
    from core.surfaces.dispatcher import route_inbound
    from surfaces._actor import InboundResult, act_on_inbound

    decision = await route_inbound(container, inbound)
    from core.surfaces.command_reply import admit_inbound_reply, reply_text
    noted = []   # the room note goes out once per inbound line

    async def safe_deliver(reply):
        if admit_inbound_reply(inbound, decision, reply):
            if not await _send_with_actions(container, inbound, reply):
                await deliver(reply_text(reply))
        elif await _owner_reply_to_dm(container, inbound, decision, reply_text(reply),
                                      deliver, deliver_dm, noted):
            return
        else:
            logger.info('Private or rate-limited inbound reply suppressed on %s', inbound.identity.source.surface_id)
    # Only a message WITH files carries a downloader: a text-only turn calls
    # the executor exactly as before.
    extra = {"fetch_media": fetch_media} if fetch_media is not None else {}
    reply = await act_on_inbound(
        task_agent, InboundResult(inbound=inbound, decision=decision),
        deliver=safe_deliver, spawn=spawn, **extra)
    if reply:
        # A command may answer with a CommandReply (a room reply, an action
        # card): a text surface delivers its words, which keep every token.
        await safe_deliver(reply)


#: The room's one line when the owner's verb was answered in his DM (F-03).
ANSWERED_IN_DM = "🔒 Answered in your DM."
#: ... and when his DM could not be reached (no address on this app, or the send
#: failed): he must not read silence, and the private answer stays out of the room.
DM_UNREACHABLE = ("🔒 That answer is private, and I could not reach your DM on "
                  "this app. Ask me again in our private chat.")


async def _owner_reply_to_dm(container: Any, inbound: Any, decision: Any, text: str,
                             deliver: Callable, deliver_dm: Optional[Callable],
                             noted: list) -> bool:
    """113 W7 F-03: the owner's verb typed in a ROOM is answered in his DM on the
    SAME app, through ``core.surfaces.owner_address``. True = handled (sent to the
    DM, or the room told it could not be); False = not the owner's room verb, the
    caller drops the reply as before. The reply itself never enters the room."""
    from core.surfaces.dispatcher import RouteKind
    src = inbound.identity.source
    if (decision.kind != RouteKind.COMMAND or src.chat_type == "dm"
            or deliver_dm is None or not text):
        return False
    from core.instance import is_owner_uid
    if not is_owner_uid(inbound.identity.user_id, allow_local=False):
        return False
    from core.surfaces.owner_address import owner_address
    address = None
    try:
        address = owner_address(container, src.surface_id, inbound.identity.user_id)
    except Exception:
        logger.warning("owner DM address unresolved on %s", src.surface_id, exc_info=True)
    sent = False
    if address:
        try:
            await deliver_dm(str(address), text)
            sent = True
        except Exception:
            logger.warning("owner DM send failed on %s", src.surface_id, exc_info=True)
    if not noted:
        noted.append(True)
        await deliver(ANSWERED_IN_DM if sent else DM_UNREACHABLE)
    return True


def _reply_actions(reply) -> list:
    """The buttons of a carded command reply (``CommandReply.card_id``), none for
    anything else. Read-only: a missing card store is not created here."""
    from core.surfaces.command_reply import CommandReply
    cid = getattr(reply, "card_id", None) if isinstance(reply, CommandReply) else None
    if not cid:
        return []
    try:
        from core.surfaces import cards
        st = cards.existing_store()
        card = st.get(cid) if st is not None else None
        return cards.card_actions(card) if card is not None else []
    except Exception:
        logger.debug("card actions unavailable (the text keeps the commands)", exc_info=True)
        return []


async def _send_with_actions(container: Any, inbound: Any, reply: Any) -> bool:
    """113 W7 F-09: a carded reply on a surface that ``supports_actions`` goes out
    through the message router WITH the card's buttons. True = sent; False = the
    caller delivers the text (it keeps every command, so nothing is lost)."""
    actions = _reply_actions(reply)
    if not actions or container is None:
        return False
    try:
        sid = inbound.identity.source.surface_id
        registry = container.get_service("surface_registry")
        surface = registry.get(sid) if registry is not None else None
        caps = getattr(surface, "capabilities", None)
        if surface is None or not getattr(caps, "supports_actions", False):
            return False
        from core.surfaces.command_reply import reply_text
        from core.surfaces.envelopes import OutboundMessage
        from core.surfaces.session_chat_registry import build_session_key
        key = build_session_key(inbound.identity.source, inbound.identity.user_id)
        # Through the router, the one recorded outbound seam (033): a chat with no
        # binding yet answers False and the caller sends the text.
        router = container.get_service("message_router")
        if router is None:
            return False
        return bool(await router.publish(OutboundMessage(
            session_key=key, text=reply_text(reply), actions=actions)))
    except Exception:
        logger.warning("buttons send failed on %s; sending the text",
                       getattr(inbound.identity.source, "surface_id", "?"), exc_info=True)
        return False


async def _hook(fn, *args) -> None:
    """Run an optional surface hook; a failing reaction never costs the turn."""
    try:
        await fn(*args)
    except Exception:
        logger.debug("surface hook %s failed", getattr(fn, "__name__", fn), exc_info=True)


class TextSink:
    """cron/delivery sink over one async ``send(chat_id, text)`` (best-effort).

    Every surface registered a ``<Name>Sink`` with this exact body; the only
    thing that differed was the client method it called.
    """

    def __init__(self, send, *, label: str, surface_id: str = "") -> None:
        self._send = send
        self._label = label
        self._surface_id = surface_id

    async def send_message(self, chat_id, text) -> bool:
        try:
            for chunk in split_for_surface(self._surface_id, str(text)):
                await self._send(str(chat_id), chunk)
            return True
        except Exception:
            logger.warning("%s.send_message failed for %s", self._label, chat_id,
                           exc_info=True)
            return False


#: The feedback marks (064 F2): "seen" while the turn runs, "failed" after a
#: turn that raised. A transport maps them to its own reaction names.
SEEN_MARK = "👀"
FAILED_MARK = "❌"


def wants_feedback(inbound) -> bool:
    """Whether a reaction on *inbound* is wanted (113 W7 F-08).

    The hook runs BEFORE routing, so it cannot see the decision. A DM is always
    addressed to the bot; in a room only a line that names the bot is — a 👀 on
    every line of room chatter would say the bot reads (and answers) all of it.
    A synthetic or bot message gets none."""
    if getattr(inbound, "internal", False) or getattr(inbound, "sender_is_bot", False):
        return False
    src = getattr(getattr(inbound, "identity", None), "source", None)
    if getattr(src, "chat_type", None) == "dm":
        return True
    return getattr(inbound, "mentions_bot", None) is True


def _storefront_buyer(inbound) -> str:
    """The storefront buyer of *inbound*: its DM chat id, or ``""`` when it is not a
    person's DM (a group, a channel, a synthetic or bot message). Fail-closed."""
    if getattr(inbound, "internal", False) or getattr(inbound, "sender_is_bot", False):
        return ""
    src = getattr(getattr(inbound, "identity", None), "source", None)
    if getattr(src, "chat_type", None) != "dm":
        return ""
    return str(getattr(src, "chat_id", "") or "")


class BaseHarness:
    """The surface-agnostic half of a chat-surface harness.

    A transport supplies parse/run/stop and ``_deliver_to``; this holds the
    container, the task agent, the dedup store and the user directory, and
    does the two steps every harness repeated by hand: drop a redelivery, then
    route the inbound through ``route_and_act`` with a best-effort
    deliver-back closure.
    """

    surface_id: str = ""

    def __init__(self, container: Any, task_agent: Any, dedup: Any) -> None:
        self._container = container
        self._task_agent = task_agent
        self._dedup = dedup
        self._user_directory = container.get_service("user_directory") \
            if container else None

    def _is_duplicate(self, inbound) -> bool:
        key = getattr(inbound, "idempotency_key", None)
        return bool(key) and self._dedup.seen(f"{self.surface_id}:{key}")

    async def _deliver_to(self, target, text: str) -> None:
        """Send *text* back to *target* on this transport."""
        raise NotImplementedError

    #: Optional ``async (address, text)`` to a person's DM, where ``address`` is
    #: the ``core.surfaces.owner_address`` shape for this surface. None = this
    #: transport cannot (or must not) open a DM, and an owner's room verb keeps
    #: its old outcome (the private reply is dropped). Opt-in per transport.
    _deliver_dm: Optional[Callable] = None

    def _dm_sender(self) -> Optional[Callable]:
        """``_deliver_dm`` split to the platform limit; raises on a failed chunk."""
        send = self._deliver_dm
        if send is None:
            return None

        async def _send(address: str, text: str) -> None:
            for chunk in split_for_surface(self.surface_id, text):
                await send(address, chunk)
        return _send

    async def _before_route(self, target) -> None:
        """Transport hook run before routing (e.g. a typing indicator)."""

    def _reply_target(self, inbound) -> Any:
        """Where a reply to *inbound* goes; handed to ``_deliver_to`` and
        ``_before_route``. Default: the chat id. A transport with threads
        (Slack, OS7) returns its own shape."""
        return inbound.identity.source.chat_id

    async def _route(self, inbound) -> None:
        target = self._reply_target(inbound)

        async def _deliver(text: str) -> None:
            # OS1: split to the platform's limit; stop at the first failed
            # chunk (the rest would arrive without its start).
            for chunk in split_for_surface(self.surface_id, text):
                try:
                    await self._deliver_to(target, chunk)
                except Exception:
                    logger.warning("%s deliver failed", self.surface_id, exc_info=True)
                    return

        # 099 W3: the storefront answers /offers, /buy, /order by code (no model, no
        # tools) while a live offer lists this surface; anything else routes as before.
        # DMs only, like Telegram: the buyer is the DM's chat (one person), never the
        # reply target — a Slack target is (channel, thread_ts), so a thread would be a
        # new buyer, and a group channel would let anyone in it read or cancel orders.
        buyer = _storefront_buyer(inbound)
        if buyer:
            from modules.commerce.storefront import storefront_reply
            reply = await storefront_reply(self._container, self.surface_id, buyer,
                                           getattr(inbound, "text", "") or "")
            if reply is not None:
                await _deliver(reply)
                return
        try:
            await self._before_route(target)
        except Exception:
            pass
        await _hook(self.on_processing_start, inbound)
        ok = False
        try:
            if await self._voice_to_text(inbound, _deliver):
                await route_and_act(self._container, self._task_agent, inbound, _deliver,
                                    fetch_media=self._media_bytes if inbound.media else None,
                                    deliver_dm=self._dm_sender())
            ok = True
        finally:
            await _hook(self.on_processing_finish, inbound, ok)

    async def _voice_to_text(self, inbound, deliver) -> bool:
        """Transcribe a voice-only message through the shared seam. False = the sender
        was told it failed, and no turn runs (the same guard as Telegram and WhatsApp).

        A sender the media gate refuses costs no download and no transcription; the
        message routes as before, and the router decides by tier."""
        from core.surfaces.config import SurfaceConfig
        from core.surfaces.media_access import paid_media_allowed, voice_bytes_too_large
        from core.surfaces.transcription import (
            VOICE_TIMEOUT_SECONDS, apply_voice_transcript, voice_present)
        from core.surfaces.voice_echo import voice_echo_message, voice_transcript
        from core.surfaces.voice_guard import voice_needs_guard, voice_unavailable_message

        if not voice_present(inbound.media) or (inbound.text or "").strip():
            return True
        if not paid_media_allowed(self._container, inbound):
            return True
        note = next(m for m in inbound.media if m.kind in ("voice", "audio"))
        try:
            async with asyncio.timeout(VOICE_TIMEOUT_SECONDS):
                if SurfaceConfig.voice_transcription_enabled() and not note.data:
                    raw = await self._fetch_media(note)
                    if raw and not voice_bytes_too_large(raw):
                        note.data = raw
                await apply_voice_transcript(self._container, inbound)
        except Exception as e:  # a timeout too: the guard below tells the sender
            logger.warning("%s voice transcription failed: %s", self.surface_id, e)
        if voice_needs_guard(inbound.media, inbound.text):
            await deliver(voice_unavailable_message(SurfaceConfig.voice_transcription_enabled()))
            return False
        if SurfaceConfig.voice_transcript_echo_enabled():
            await deliver(voice_echo_message(voice_transcript(inbound.media) or ""))
        return True

    async def _media_bytes(self, media) -> Optional[bytes]:
        """The bytes of one attachment; a transcribed note is not downloaded twice."""
        return getattr(media, "data", None) or await self._fetch_media(media)

    async def on_processing_start(self, inbound) -> None:
        """064 F2: optional "seen" reaction (👀) when a message is accepted.
        No-op by default; a transport that can react overrides it, and asks
        :func:`wants_feedback` first. Slack and Discord do."""

    async def on_processing_finish(self, inbound, ok: bool) -> None:
        """064 F2: optional finish mark: the 👀 goes, a failed turn gets ❌ (the
        reply itself is the success mark). No-op by default."""

    async def _fetch_media(self, media) -> Optional[bytes]:
        """Bytes for one inbound attachment (064 F3). A transport overrides it;
        the default fetches nothing, so the executor NAMES the file instead."""
        return None


def register_surface_and_sink(container: Any, surface: Any, *, sink_name: str,
                              send, sink_label: str) -> None:
    """030 WS-B2: ``register_surface`` enforces the contract, joins the surface
    registry (so ``surface_profile()`` reaches the prompt) AND subscribes to the
    router; the cron/delivery sink is registered once beside it."""
    if container is None:
        return
    from core.surfaces.registry import register_surface
    register_surface(container, surface)
    if container.get_service(sink_name) is None:
        container.register_service(sink_name, TextSink(
            send, label=sink_label, surface_id=str(getattr(surface, "surface_id", "") or "")))


async def fetch_capped(url: str, *, allowed_hosts: tuple,
                       headers: Optional[dict] = None,
                       max_bytes: Optional[int] = None) -> Optional[bytes]:
    """GET one platform attachment, or None (064 F3).

    Two guards every surface needs, so they live once:

    * ``https`` to a host in ``allowed_hosts`` only (a suffix match on a dot
      boundary). The attachment URL comes from the platform's event, and Slack's
      needs the BOT TOKEN in the header — a URL pointed anywhere else would hand
      that token to a stranger, or turn the bot into a fetcher of internal hosts.
    * the size cap (``INBOUND_MEDIA_MAX_MB``) enforced WHILE reading, so a lying
      ``size`` field cannot make the process buffer a huge body.

    It opens its OWN session: a surface client's session may carry the bot
    token as a default header (Discord's does), and only the headers passed
    here may leave. None = not fetched; the executor names the file.
    """
    import aiohttp
    from urllib.parse import urlparse

    from core.surfaces.inbound_attachments import inbound_media_max_mb
    cap = max_bytes if max_bytes is not None else int(inbound_media_max_mb() * 1024 * 1024)
    parts = urlparse(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(
            host == h or host.endswith("." + h) for h in allowed_hosts):
        logger.warning("inbound media: refused fetch from %r (not an allowed host)", host)
        return None
    async with aiohttp.ClientSession() as session, \
            session.get(url, headers=headers or {}, allow_redirects=False) as resp:
        if resp.status != 200:
            logger.warning("inbound media: %s answered HTTP %s", host, resp.status)
            return None
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > cap:
            logger.warning("inbound media: %s bytes over the %s-byte cap", declared, cap)
            return None
        chunks, size = [], 0
        async for chunk in resp.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > cap:
                logger.warning("inbound media: body passed the %s-byte cap", cap)
                return None
            chunks.append(chunk)
        return b"".join(chunks)
