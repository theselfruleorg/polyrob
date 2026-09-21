"""MessageRouter: the SINGLE outbound producer seam.

Every agent->user emit funnels through publish(). Text is brain-scrubbed here
(SSOT = modules.llm.brain_scrubber) so every surface inherits the scrub. Surfaces
subscribe by surface_id; routing resolves session_key -> surface_id via the
SessionChatRegistry. Fail-open: an unroutable key or a raising surface never
crashes the agent loop.
"""
import logging
from dataclasses import replace
from typing import Optional

from core.config_policy import dead_target_registry_enabled
from core.surfaces.dead_targets import ROOM_DEATH_REASONS, classify_dead_error
from core.surfaces.envelopes import OutboundMessage
from core.surfaces.room_keys import is_group_session_key
from core.surfaces.session_chat_registry import (
    SessionChatRegistry, row_from_session_key,
)
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
        # 044 T11: bounded room traffic (per-chat reply cap). None by default =
        # no gating, byte-identical legacy. Injected post-construction via
        # attach_room_caps, mirrors attach_dead_targets.
        self._room_caps = None
        # 2026-09-16: the room log, so a delivered ROOM reply is recorded as the
        # agent's own line. None = no recording, byte-identical legacy. Injected
        # post-construction via attach_room_ledger, mirrors attach_room_caps.
        self._room_ledger = None

    def attach_room_ledger(self, ledger) -> None:
        """Bind the room log so every delivered room reply is recorded.

        Separate from the ingest path on purpose: this is the ONLY place that
        knows a room reply actually reached the surface, and an undelivered
        reply must never be written as something the room heard.
        """
        self._room_ledger = ledger

    def _queue_accepted(self, idem: str, **row) -> bool:
        """Enqueue *row* and answer honestly whether the queue now owns it.

        ``OutboundDeliveryQueue.enqueue`` is an ``INSERT OR IGNORE``, so False
        means "a row under this key already exists". That is acceptance when
        the existing row is pending/inflight/delivered and a LIE when it was
        dead-lettered — the queue gave up on that body and nothing will retry
        it (D5, 2026-09-21 interface audit; the dead-letter case is precisely
        the one the 24h dedup then made permanent).
        """
        if self._queue.enqueue(idempotency_key=idem, **row):
            return True
        probe = getattr(self._queue, "accepted", None)
        if not callable(probe):
            # A queue that cannot be asked keeps the legacy contract: enqueue
            # did not raise, so it is treated as accepted.
            return True
        try:
            return bool(probe(idem))
        except Exception as e:
            logger.warning("outbound queue state probe failed for %s: %s",
                           idem, e, exc_info=True)
            return False

    def _record_room_reply(self, msg, surface_id, chat_id, text: str) -> None:
        """Write a DELIVERED room reply into the room log. Fail-open."""
        if self._room_ledger is None or not is_group_session_key(msg.session_key):
            return
        try:
            from core.surfaces.ledger_ingest import record_outbound_to_ledger
            import time as _t
            # The thread rides on the KEY (`…:{chat_id}:thread:{id}`, see
            # session_chat_registry.build_session_key) and is NOT a field of
            # `row_from_session_key`. It has to match what the room turn wrote on
            # the way in, because `tail` filters `AND thread_id=?` — record it on
            # the wrong thread and the agent's own line is invisible to the very
            # context block that needs it.
            _parts = str(msg.session_key or "").split(":thread:")
            _thread = _parts[1] if len(_parts) == 2 and _parts[1] else None
            record_outbound_to_ledger(
                self._room_ledger, surface=surface_id, chat_id=chat_id,
                thread_id=_thread, text=text, ts=_t.time(),
                reply_to=msg.reply_to, media=bool(msg.media))
        except Exception as e:  # never let bookkeeping undo a delivery
            logger.debug("room reply not recorded (fail-open): %s", e)

    def attach_queue(self, q) -> None:
        """Attach a durable OutboundDeliveryQueue. Call from bootstrap after construction."""
        self._queue = q

    def attach_dead_targets(self, dt) -> None:
        """Attach a DeadTargetStore. Call from bootstrap after construction (mirrors
        attach_queue)."""
        self._dt = dt

    def attach_room_caps(self, room_caps) -> None:
        """Attach a RoomCaps store. Call from bootstrap after construction (mirrors
        attach_dead_targets)."""
        self._room_caps = room_caps

    def _data_dir(self) -> Optional[str]:
        """The data home this bus was built in — ``surfaces.db``'s directory.

        044 T21: the room allowlist lives beside it (``group_allowlist.db``).
        Derived from the registry rather than re-resolved from the environment,
        so the router can never mark a room left in a DIFFERENT data home than
        the one it is routing for. None (unknown registry shape) falls back to
        the ordinary resolution in ``room_keys``.
        """
        import os
        path = getattr(self._registry, "db_path", None)
        return os.path.dirname(path) or "." if path else None

    def _is_room(self, surface_id, chat_id) -> bool:
        """Is this destination an allowlisted ROOM? Fail-closed (reads as "no"),
        so an unreadable allowlist never invents a room departure."""
        import types
        from core.surfaces.room_keys import is_room_target
        return is_room_target(
            types.SimpleNamespace(config=types.SimpleNamespace(
                data_dir=self._data_dir())), surface_id, chat_id)

    def _mark_room_left(self, surface_id, chat_id, reason: str) -> None:
        """044 T21: the bot is no longer IN this room — record it where the OWNER
        looks (``/groups list``, ``polyrob doctor``) as ``left``, not ``revoked``
        (which would claim HE withdrew permission). Ingress stops too (``left``
        is not ``active``) and a re-``allow`` restores the room after a rejoin.
        Fail-open: bookkeeping never takes down a delivery path."""
        from core.surfaces.room_keys import mark_room_left
        if mark_room_left(self._data_dir(), surface_id, chat_id):
            logger.warning("message_router: room %s:%s marked LEFT (%s)",
                           surface_id, chat_id, reason)

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

    async def publish(self, msg: OutboundMessage) -> bool:
        """Route one agent message to its bound surface.

        Returns True when the text was DELIVERED — a completed direct send, or
        durable acceptance into the cross-process outbound queue — and False on
        every drop, suppression, cap, dead target or failure. 044 T20 fix round 1
        (Minor 7): the discrete mirror records a room reply's `answered_by` from
        this answer, so a suppressed `[SILENT]` or a capped post must not be
        recorded as an answer the room received.
        """
        try:
            scrubbed = scrub_brain_blocks(msg.text)
        except Exception:  # fail-open: never drop a reply over a scrub bug
            scrubbed = msg.text
        # F3 parity with HITLManager.stream_output: a wholly-brain (or empty) chunk
        # scrubs to None/"" and must be DROPPED, not delivered as an empty bubble.
        if scrubbed is None or not scrubbed.strip():
            return False
        if scrubbed != msg.text:
            msg = OutboundMessage(
                session_key=msg.session_key, text=scrubbed, kind=msg.kind,
                partial=msg.partial, stream_id=msg.stream_id,
                reply_to=msg.reply_to, media=msg.media,
            )
        # 044 T15: two rules that apply only when the audience is a ROOM. Run
        # AFTER the brain scrub, so a `[SILENT]` wrapped in a brain block is
        # still silence.
        if is_group_session_key(msg.session_key):
            # `[SILENT]` is the agent's "nothing here needs an answer" — in a
            # room that must cost NO message, or an `active` judgement of silence
            # becomes a public non-sequitur. EXACT match only (044 §4.4),
            # deliberately stricter than cron's "anywhere in the result" rule:
            # here one quoted token would swallow a genuine public answer.
            if (msg.text or "").strip().upper() == "[SILENT]":
                logger.info("room reply suppressed: [SILENT]")
                return False
            # A room is many humans, so a secret shape that survived every other
            # scrub must not be the thing the agent posts publicly. Same battery
            # the room LOG already applies on the way in (group_ledger.append).
            from core.secret_scrub import scrub_secret_shapes
            _safe = scrub_secret_shapes(msg.text or "")
            if _safe != msg.text:
                logger.warning("room reply: redacted a secret shape before delivery")
                msg = replace(msg, text=_safe)
                # ⚠️ `scrubbed` — NOT `msg.text` — is what the DURABLE queue path
                # below enqueues as its payload (and hashes into the idempotency
                # key). Leaving it stale would have redacted the direct send and
                # delivered the secret verbatim through the queue.
                scrubbed = _safe
        row = self._registry.resolve(msg.session_key)
        if not row:
            # 044 T20 fix round 2 (N1): a room the owner allowlisted but that has
            # never run a LIVE turn has no durable row (bind_chat_surface is the
            # only writer), so a SERVICE run there paid for the model call and
            # then had every reply dropped here, silently, while its checkpoint
            # advanced. The key IS the address — read it back rather than drop.
            # Deliberately does NOT write the row: the live session owns that.
            row = row_from_session_key(msg.session_key)
            if not row:
                logger.debug("message_router: no binding for %s; dropping", msg.session_key)
                return False
            logger.warning("message_router: no chat row for %s; delivering by key "
                           "(%s:%s)", msg.session_key, row["surface_id"], row["chat_id"])
        surface = self._surfaces.get(row.get("surface_id"))
        if surface is None:
            logger.debug("message_router: no surface %s subscribed", row.get("surface_id"))
            return False
        surface_id = row.get("surface_id")
        chat_id = row.get("chat_id")
        # T1.5 / 044 T21: skip a provably-dead target (bot blocked, chat deleted,
        # bot kicked from the room) FIRST — before the room cap and before the
        # durable queue. Gating it later meant a kicked room still burned its
        # hourly reply budget and still piled up outbox rows nothing could ever
        # deliver. Read-only indexed lookup; is_dead() is itself fail-open on any
        # store error, so this can never turn into a hard failure.
        if (self._dt is not None and dead_target_registry_enabled()
                and self._dt.is_dead(surface_id, chat_id or "")):
            logger.info("message_router: dead-target SKIP surface=%s dest=%s",
                        surface_id, chat_id)
            return False
        # 044 T11 (fix round 1, finding #2): bounded room traffic — a streamed delta
        # doesn't count (only the committed reply does) and this never touches a DM.
        # `may_reply` gates the ATTEMPT here; `record_reply` only fires once delivery
        # actually SUCCEEDS (durable-queue acceptance below, or a direct
        # SendResult(success=True) further down) — a failed/skipped/dead-target
        # delivery must never consume the hourly budget for nothing.
        _room_gated = (not msg.partial and self._room_caps is not None
                      and is_group_session_key(msg.session_key))
        if _room_gated:
            ok, why = self._room_caps.may_reply(surface_id, chat_id)
            if not ok:
                logger.warning("room reply suppressed: %s", why)
                return False
        # Durable path (final messages only): enqueue instead of sending directly.
        from core.surfaces.config import SurfaceConfig
        if (self._queue is not None and not msg.partial
                and SurfaceConfig.outbound_queue_enabled()):
            turn = msg.stream_id or msg.session_key
            idem = f"{msg.session_key}#{turn}#{hash(scrubbed) & 0xffffffff}"
            accepted = False
            try:
                accepted = self._queue_accepted(
                    idem, session_key=msg.session_key, surface_id=surface_id,
                    dest=chat_id, payload=scrubbed,
                    kind=str(getattr(msg.kind, "value", msg.kind)),
                    media=msg.media or None,  # 030 L4: media rides the queue row
                )
            except Exception as e:  # fail-open: fall back to a direct send on a queue fault
                logger.error("outbound enqueue failed, sending directly: %s", e)
            else:
                if accepted:
                    if _room_gated:
                        self._room_caps.record_reply(surface_id, chat_id)
                    self._record_room_reply(msg, surface_id, chat_id, scrubbed)
                    return True  # durable acceptance IS delivery (dispatcher retries)
                # D5: the enqueue was a no-op AND no live row stands behind the
                # key — the previous row was dead-lettered. Reporting that as
                # delivered is the exact lie this branch used to tell; fall
                # through to a direct send instead.
                logger.warning(
                    "outbound enqueue no-op for %s (dead-lettered or unreadable row) "
                    "— sending directly", idem)
        try:
            if msg.partial:
                await surface.stream(msg)  # base buffers if surface can't stream
                # A live-edit surface has already opened a user-visible bubble;
                # a buffered surface has not.  The stream producer uses this
                # distinction to avoid letting done() publish a second recap.
                # ``getattr`` keeps legacy/test surface doubles (which only
                # implemented ``stream``) compatible; real Surface subclasses
                # inherit ``stream_is_live`` above.
                return bool(getattr(surface, "stream_is_live", lambda: False)())
            else:
                result = await surface.send(msg)
                ok = bool(getattr(result, "success", False))
                if ok and _room_gated:
                    self._room_caps.record_reply(surface_id, chat_id)
                if ok:
                    self._record_room_reply(msg, surface_id, chat_id, scrubbed)
                if self._dt is not None and dead_target_registry_enabled():
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
                            # 044 T21: a whole-chat death on a ROOM key means the
                            # bot is no longer IN that room. Record it where the
                            # OWNER looks (`/groups list`) as `left` — not
                            # `revoked`, which would claim he withdrew permission
                            # — so a room that went quiet is explained instead of
                            # mysterious. Ingress stops too (`left` != `active`),
                            # and a re-`allow` restores it after a rejoin.
                            if (is_group_session_key(msg.session_key)
                                    and reason in ROOM_DEATH_REASONS):
                                self._mark_room_left(surface_id, chat_id, reason)
                return ok
        except Exception as e:  # fail-open
            logger.error("message_router: surface %s raised: %s", row.get("surface_id"), e, exc_info=True)
            return False

    async def send_message(self, chat_id: str, text: str, surface_id: str = "telegram",
                            media: list | None = None,
                            subject: str | None = None) -> bool:
        """Boolean shim over :meth:`send_message_ex` (the legacy contract).

        ``True`` covers BOTH a completed direct send and durable acceptance into
        the cross-process queue. A caller that must tell those apart — the user
        delivery rail does, because "queued" is not "the owner read it" — calls
        ``send_message_ex`` instead.
        """
        return await self.send_message_ex(
            chat_id, text, surface_id, media=media, subject=subject) != "failed"

    async def send_message_ex(self, chat_id: str, text: str,
                              surface_id: str = "telegram",
                              media: list | None = None,
                              subject: str | None = None) -> str:
        """Proactive send. Returns ``"sent"`` | ``"queued"`` | ``"failed"``.

        Back-compat shim for cron/delivery.py + the `message` tool. `media`
        defaults to None -> OutboundMessage(media=[]), keeping today's shape
        byte-identical when no media is given. ``subject`` (D70) rides as the
        legacy ``{"subject": ...}`` media entry an email surface reads; every
        other surface ignores it.

        ``"queued"`` is the honest name for what used to be reported as a send:
        the message was handed to the durable queue for another process to
        deliver. The rail records that as a FALLBACK, never as ``sent``,
        because a queued body can still dead-letter and the 24h dedup would
        otherwise make that loss permanent (D6, 2026-09-21 interface audit).

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
        if subject:
            # The legacy email-subject entry (never a renderable media entry).
            media = [{"subject": str(subject)}] + list(media or [])
        surface = self._surfaces.get(surface_id)
        if surface is None:
            # 2026-08-30: this cross-process fallback is keyed on queue EXISTENCE
            # only, not OUTBOUND_QUEUE_ENABLED — that flag governs publish()'s
            # primary reply-routing decision (see bootstrap.py), a different and
            # separately-risky behavior change. The queue/dispatcher are now built
            # unconditionally (bootstrap.py), so this fallback works regardless of
            # that flag's setting.
            if self._queue is not None:
                accepted = False
                try:
                    idem = f"direct:{surface_id}:{chat_id}#{hash(text) & 0xffffffff}"
                    accepted = self._queue_accepted(
                        idem, session_key=f"direct:{surface_id}:{chat_id}",
                        surface_id=surface_id, dest=chat_id, payload=text,
                        kind="message", media=media or None,
                    )
                except Exception as e:
                    logger.error("send_message: queue enqueue fallback failed: %s", e)
                else:
                    if accepted:
                        logger.info(
                            "send_message: surface %s not local — enqueued for "
                            "cross-process delivery", surface_id)
                        return "queued"
                    # D5: no live row stands behind the key (it dead-lettered),
                    # and there is no local surface to fall through to.
                    logger.warning(
                        "send_message: enqueue no-op for %s — the queued copy was "
                        "dead-lettered; delivery failed", idem)
            logger.warning("send_message: no surface %s registered — delivery failed", surface_id)
            return "failed"
        if (self._dt is not None and dead_target_registry_enabled()
                and self._dt.is_dead(surface_id, chat_id or "")):
            logger.info("send_message: dead-target SKIP surface=%s dest=%s", surface_id, chat_id)
            return "failed"
        try:
            result = await surface.send(OutboundMessage(
                session_key=f"direct:{surface_id}:{chat_id}", text=text,
                media=media or [],
            ))
        except Exception as e:
            logger.error("send_message shim failed: %s", e, exc_info=True)
            return "failed"
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
                    # 044 T21: this shim's synthetic `direct:` key carries no
                    # chat_type, so the room test is the ALLOWLIST, not the key —
                    # a cron report or a `message` post into a room the bot was
                    # kicked from must mark it left exactly as a room reply does.
                    if reason in ROOM_DEATH_REASONS and self._is_room(surface_id, chat_id):
                        self._mark_room_left(surface_id, chat_id, reason)
            return "failed"
        return "sent"
