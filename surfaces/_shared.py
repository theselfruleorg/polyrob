"""Shared polled-inbound tail (030 WS-B3/E2).

The four polling harnesses (discord, slack, signal, x) carried byte-similar
``_route`` bodies: route the inbound, build a deliver closure, run the shared
executor, deliver the immediate reply. ONE tail here; each harness keeps only
its transport-specific ``deliver`` (and optional typing) closure.

Lives in the surfaces tier (not core/) because it reaches the shared inbound
executor through ``surfaces._actor`` — core must never import surfaces
(layering ratchet).
"""
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


async def route_and_act(container: Any, task_agent: Any, inbound: Any,
                        deliver: Callable, *, spawn: Optional[Callable] = None,
                        fetch_media: Optional[Callable] = None) -> None:
    """route_inbound → act_on_inbound(deliver=, fetch_media=) → deliver the reply.

    ``fetch_media`` (064 F3) is the surface's async ``(Media) -> bytes | None``.
    The shared executor decides by TIER what happens to the files: the owner's
    are stored in the session workspace, a correspondent's are only named.
    """
    from core.surfaces.dispatcher import route_inbound
    from surfaces._actor import InboundResult, act_on_inbound

    decision = await route_inbound(container, inbound)
    # Only a message WITH files carries a downloader: a text-only turn calls
    # the executor exactly as before.
    extra = {"fetch_media": fetch_media} if fetch_media is not None else {}
    reply = await act_on_inbound(
        task_agent, InboundResult(inbound=inbound, decision=decision),
        deliver=deliver, spawn=spawn, **extra)
    if reply:
        # A command may answer with a CommandReply (a room reply, an action
        # card): a text surface delivers its words, which keep every token.
        from core.surfaces.command_reply import reply_text
        await deliver(reply_text(reply))


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

    def __init__(self, send, *, label: str) -> None:
        self._send = send
        self._label = label

    async def send_message(self, chat_id, text) -> bool:
        try:
            await self._send(str(chat_id), str(text))
            return True
        except Exception:
            logger.warning("%s.send_message failed for %s", self._label, chat_id,
                           exc_info=True)
            return False


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

    async def _before_route(self, target) -> None:
        """Transport hook run before routing (e.g. a typing indicator)."""

    async def _route(self, inbound) -> None:
        target = inbound.identity.source.chat_id

        async def _deliver(text: str) -> None:
            try:
                await self._deliver_to(target, text)
            except Exception:
                logger.warning("%s deliver failed", self.surface_id, exc_info=True)

        try:
            await self._before_route(target)
        except Exception:
            pass
        await _hook(self.on_processing_start, inbound)
        ok = False
        try:
            await route_and_act(self._container, self._task_agent, inbound, _deliver,
                                fetch_media=self._fetch_media if inbound.media else None)
            ok = True
        finally:
            await _hook(self.on_processing_finish, inbound, ok)

    async def on_processing_start(self, inbound) -> None:
        """064 F2: optional "seen" reaction (👀) when a message is accepted.
        No-op by default; a transport that can react overrides it."""

    async def on_processing_finish(self, inbound, ok: bool) -> None:
        """064 F2: optional finish reaction (✅ / ❌). No-op by default."""

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
        container.register_service(sink_name, TextSink(send, label=sink_label))


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
