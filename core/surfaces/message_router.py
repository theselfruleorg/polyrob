"""MessageRouter: the SINGLE outbound producer seam.

Every agent->user emit funnels through publish(). Text is brain-scrubbed here
(SSOT = modules.llm.brain_scrubber) so every surface inherits the scrub. Surfaces
subscribe by surface_id; routing resolves session_key -> surface_id via the
SessionChatRegistry. Fail-open: an unroutable key or a raising surface never
crashes the agent loop.
"""
import logging
from typing import Optional

from core.config_policy import dead_target_registry_enabled
from core.surfaces.dead_targets import classify_dead_error
from core.surfaces.envelopes import OutboundMessage
from core.surfaces.session_chat_registry import SessionChatRegistry
from modules.llm.brain_scrubber import scrub_brain_blocks

# TYPE_CHECKING import avoids a circular-import risk; the store is pure.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.surfaces.dead_targets import DeadTargetStore

logger = logging.getLogger(__name__)


class MessageRouter:
    def __init__(self, registry: SessionChatRegistry, *,
                 dead_targets: Optional["DeadTargetStore"] = None) -> None:
        self._registry = registry
        self._surfaces: dict[str, object] = {}
        self._queue = None
        # T1.5: dead-target gate for the DIRECT send path (the durable-queue path
        # is gated by OutboundDispatcher instead). None by default = no gating,
        # byte-identical legacy. Injected post-construction via attach_dead_targets
        # (mirrors attach_queue) or the ctor kwarg above.
        self._dt = dead_targets

    def attach_queue(self, q) -> None:
        """Attach a durable OutboundDeliveryQueue. Call from bootstrap after construction."""
        self._queue = q

    def attach_dead_targets(self, dt) -> None:
        """Attach a DeadTargetStore. Call from bootstrap after construction (mirrors
        attach_queue)."""
        self._dt = dt

    def subscribe(self, surface_id: str, surface) -> None:
        self._surfaces[surface_id] = surface

    def capabilities(self, surface_id: str):
        """Capabilities of a subscribed surface, or None if unknown (mirrors
        SurfaceRegistry.capabilities). Used by callers (e.g. the `message` action)
        that need to know ahead of a send whether a surface can render media."""
        surface = self._surfaces.get(surface_id)
        return getattr(surface, "capabilities", None) if surface is not None else None

    def bot_username(self, surface_id: str):
        """The subscribed surface's own bot handle (no ``@``), or None if unknown.
        Lets a caller recognize "the agent addressed its own handle" (e.g. an
        owner-notify `message` action that mistakenly used its own @username as
        the target) without hardcoding any deployment-specific username."""
        surface = self._surfaces.get(surface_id)
        return getattr(surface, "bot_username", None) if surface is not None else None

    async def publish(self, msg: OutboundMessage) -> None:
        try:
            scrubbed = scrub_brain_blocks(msg.text)
        except Exception:  # fail-open: never drop a reply over a scrub bug
            scrubbed = msg.text
        # F3 parity with HITLManager.stream_output: a wholly-brain (or empty) chunk
        # scrubs to None/"" and must be DROPPED, not delivered as an empty bubble.
        if scrubbed is None or not scrubbed.strip():
            return
        if scrubbed != msg.text:
            msg = OutboundMessage(
                session_key=msg.session_key, text=scrubbed, kind=msg.kind,
                partial=msg.partial, stream_id=msg.stream_id,
                reply_to=msg.reply_to, media=msg.media,
            )
        row = self._registry.resolve(msg.session_key)
        if not row:
            logger.debug("message_router: no binding for %s; dropping", msg.session_key)
            return
        surface = self._surfaces.get(row.get("surface_id"))
        if surface is None:
            logger.debug("message_router: no surface %s subscribed", row.get("surface_id"))
            return
        # Durable path (final messages only): enqueue instead of sending directly.
        from core.surfaces.config import SurfaceConfig
        if (self._queue is not None and not msg.partial
                and SurfaceConfig.outbound_queue_enabled()):
            turn = msg.stream_id or msg.session_key
            idem = f"{msg.session_key}#{turn}#{hash(scrubbed) & 0xffffffff}"
            try:
                self._queue.enqueue(
                    idempotency_key=idem, session_key=msg.session_key,
                    surface_id=row.get("surface_id"), dest=row.get("chat_id"),
                    payload=scrubbed, kind=str(getattr(msg.kind, "value", msg.kind)),
                    media=msg.media or None,  # 030 L4: media rides the queue row
                )
            except Exception as e:  # fail-open: fall back to a direct send on a queue fault
                logger.error("outbound enqueue failed, sending directly: %s", e)
            else:
                return
        surface_id = row.get("surface_id")
        chat_id = row.get("chat_id")
        # T1.5: skip a direct send to a provably-dead target (bot blocked / chat
        # deleted). Read-only indexed lookup; is_dead() is itself fail-open on any
        # store error, so this can never turn into a hard failure.
        if (self._dt is not None and dead_target_registry_enabled()
                and self._dt.is_dead(surface_id, chat_id or "")):
            logger.info("message_router: dead-target SKIP surface=%s dest=%s",
                        surface_id, chat_id)
            return
        try:
            if msg.partial:
                await surface.stream(msg)  # base buffers if surface can't stream
            else:
                result = await surface.send(msg)
                if self._dt is not None and dead_target_registry_enabled():
                    ok = bool(getattr(result, "success", False))
                    if not ok:
                        reason = classify_dead_error(surface_id, getattr(result, "error", None))
                        if reason:
                            # Own try/except: a store fault here is a dead-target-store
                            # problem, NOT the surface misbehaving — must not be folded
                            # into the outer "surface raised" log below (misattribution).
                            try:
                                self._dt.mark(surface_id, chat_id or "", reason)
                                logger.info(
                                    "message_router: dead-target MARK surface=%s dest=%s reason=%s",
                                    surface_id, chat_id, reason,
                                )
                            except Exception as mark_exc:
                                logger.error(
                                    "message_router: dead-target mark failed surface=%s dest=%s: %s",
                                    surface_id, chat_id, mark_exc,
                                )
        except Exception as e:  # fail-open
            logger.error("message_router: surface %s raised: %s", row.get("surface_id"), e, exc_info=True)

    async def send_message(self, chat_id: str, text: str, surface_id: str = "telegram",
                            media: list | None = None) -> bool:
        """Back-compat shim for cron/delivery.py + the `message` tool. Returns True on
        a completed direct send, OR on durable acceptance into the cross-process
        outbound queue (see below). `media` defaults to None -> OutboundMessage(media=[]),
        keeping today's shape byte-identical when no media is given.

        2026-08-28: a surface not hosted in THIS process (e.g. the agent daemon
        calling `surface_id="email"`, which only `polyrob-email.service` subscribes
        locally) used to be an immediate, guaranteed False — `publish()` already had a
        durable-queue path for exactly this cross-process case but this method never
        used it, so a `message(surface="email")` call from an autonomous goal could
        never succeed even though the SAME shared `outbox.db` queue, drained by every
        surface daemon's own `OutboundDispatcher`, was right there. Now falls back to
        enqueuing when the surface isn't local: True here means "handed off for
        durable, retried delivery by whichever process owns that surface," not
        "confirmed sent by THIS process" — an honest weaker guarantee, not a fake one
        (the dispatcher's own retry/backoff/dead-letter still applies).

        2026-08-30: this fallback no longer requires `OUTBOUND_QUEUE_ENABLED` — that
        flag was ALSO gating whether the queue/dispatcher existed at all
        (bootstrap.py), so with it off (the prod default) this fallback was
        permanently dead code and every cross-process send just failed outright.
        The queue is now built unconditionally; `OUTBOUND_QUEUE_ENABLED` still
        governs ONLY `publish()`'s primary reply-routing decision (direct-send vs.
        queued-with-retry for a locally-subscribed surface), which is unchanged.

        T1.5: gated by the dead-target registry (a provably-dead target is skipped
        without ever calling the surface, one info log) and result-aware — a
        ``SendResult(success=False)`` is classified + marked dead (same idiom as
        ``publish()``) and this returns False, so a proactive send failure is visible
        to the caller instead of being reported as a silent success. A return value
        with no ``success`` attribute (legacy test doubles / non-SendResult returns)
        still counts as success, preserving the pre-existing contract for callers that
        don't return a typed result."""
        surface = self._surfaces.get(surface_id)
        if surface is None:
            # 2026-08-30: this cross-process fallback is keyed on queue EXISTENCE
            # only, not OUTBOUND_QUEUE_ENABLED — that flag governs publish()'s
            # primary reply-routing decision (see bootstrap.py), a different and
            # separately-risky behavior change. The queue/dispatcher are now built
            # unconditionally (bootstrap.py), so this fallback works regardless of
            # that flag's setting.
            if self._queue is not None:
                try:
                    idem = f"direct:{surface_id}:{chat_id}#{hash(text) & 0xffffffff}"
                    self._queue.enqueue(
                        idempotency_key=idem, session_key=f"direct:{surface_id}:{chat_id}",
                        surface_id=surface_id, dest=chat_id, payload=text,
                        kind="message", media=media or None,
                    )
                except Exception as e:
                    logger.error("send_message: queue enqueue fallback failed: %s", e)
                else:
                    logger.info(
                        "send_message: surface %s not local — enqueued for cross-process "
                        "delivery", surface_id)
                    return True
            logger.warning("send_message: no surface %s registered — delivery failed", surface_id)
            return False
        if (self._dt is not None and dead_target_registry_enabled()
                and self._dt.is_dead(surface_id, chat_id or "")):
            logger.info("send_message: dead-target SKIP surface=%s dest=%s", surface_id, chat_id)
            return False
        try:
            result = await surface.send(OutboundMessage(
                session_key=f"direct:{surface_id}:{chat_id}", text=text,
                media=media or [],
            ))
        except Exception as e:
            logger.error("send_message shim failed: %s", e, exc_info=True)
            return False
        if getattr(result, "success", True) is False:
            if self._dt is not None and dead_target_registry_enabled():
                reason = classify_dead_error(surface_id, getattr(result, "error", None))
                if reason:
                    try:
                        self._dt.mark(surface_id, chat_id or "", reason)
                        logger.info(
                            "send_message: dead-target MARK surface=%s dest=%s reason=%s",
                            surface_id, chat_id, reason,
                        )
                    except Exception as mark_exc:
                        logger.error(
                            "send_message: dead-target mark failed surface=%s dest=%s: %s",
                            surface_id, chat_id, mark_exc,
                        )
            return False
        return True
