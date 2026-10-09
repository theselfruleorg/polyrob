"""WebhookSurface: the shared inbound contract for push-based platforms (WhatsApp,
BlueBubbles, Telegram-webhook). Owns: body cap -> signature verify -> parse -> dedup ->
route -> act. A concrete surface implements only verify/parse/idempotency_key. Inline
parse/route faults are logged, never raised to the HTTP layer. Journaled drains instead
retain failed work for recovery or redelivery after the HTTP acknowledgement.

Contract v2 (064 F4):

- ``handle_post`` / ``verify_challenge`` may return a :class:`WebhookResponse`, which
  ``api/webhooks.py`` renders VERBATIM (status, content type, body). A plain dict keeps
  the ``{"ok": …}`` JSON answer. A Feishu ``url_verification`` echo, a WeCom ``echostr``,
  a Kakao skill response or a WeChat OA passive XML reply needs that.
- ``decode_payload(payload, headers)`` (064 order 0003): turns the parsed body into the
  platform's plain event ONCE, between the JSON parse and ``answer_post`` — an encrypted
  webhook (Feishu Encrypt Key ``{"encrypt": …}``, WeCom AES) decrypts here, so neither
  ``answer_post`` nor ``parse`` decrypts twice. ``None`` refuses the request: no answer,
  no turn. A journal replay decodes again (the journal keeps the raw verified body).
- ``answer_post(payload)``: a surface's in-band challenge — runs AFTER the signature check.
- Body cap (``body_cap_bytes``, from the surface's catalog row, 64 KB default) BEFORE the
  signature check; signature BEFORE the JSON parse.
- Persist-before-ack (``ack_before_turn = True`` + an :class:`AcceptedEventJournal`): the
  verified raw event is journaled, the ack returns, a background drain runs the turn, and
  :meth:`recover` replays a leftover once after a restart. Default OFF — WhatsApp keeps
  its inline processing byte-for-byte.
"""
import asyncio
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, List, Optional, Union

from core.surfaces.envelopes import InboundMessage
from core.surfaces.idempotency import AcceptedEventJournal, IdempotencyStore
from core.surfaces.dispatcher import route_inbound
from core.surfaces.serialize import KeyedLock
from core.surfaces.session_chat_registry import build_session_key
from core.surfaces.transcription import voice_present, transcribe_inbound_media
from core.surfaces.voice_guard import voice_needs_guard, voice_unavailable_message
from core.surfaces.voice_echo import voice_transcript, voice_echo_message
from core.surfaces.config import SurfaceConfig
from core.surfaces.act import InboundResult, act_on_inbound  # actor registered via surfaces._actor

logger = logging.getLogger(__name__)

#: The pre-auth body cap for a surface with no catalog row.
DEFAULT_BODY_CAP_BYTES = 64 * 1024


@dataclass(frozen=True)
class WebhookResponse:
    """An HTTP answer rendered verbatim by ``api/webhooks.py``."""
    status: int = 200
    content_type: str = "application/json"
    body: Union[bytes, str] = b""

    @classmethod
    def json(cls, obj: Any, status: int = 200) -> "WebhookResponse":
        return cls(status, "application/json", json.dumps(obj))

    @classmethod
    def text(cls, text: str, status: int = 200) -> "WebhookResponse":
        return cls(status, "text/plain", text)


def body_cap_for(surface_id: str) -> int:
    """The pre-auth request-body cap from the surface's catalog row."""
    from core.surfaces.catalog import get
    spec = get(surface_id)
    return spec.body_cap_bytes if spec is not None else DEFAULT_BODY_CAP_BYTES


class WebhookSurface(ABC):
    #: 064 F4: journal the verified event, ack, then run the turn in the
    #: background. Needs ``journal``; a 5-second-ack platform sets True.
    ack_before_turn: bool = False

    def __init__(self, idempotency: IdempotencyStore,
                 journal: Optional[AcceptedEventJournal] = None) -> None:
        self._idem = idempotency
        self._journal = journal
        self._lock = KeyedLock()
        self._tasks: set = set()

    @property
    @abstractmethod
    def surface_id(self) -> str: ...

    @abstractmethod
    def verify_signature(self, headers: dict, body: bytes) -> bool: ...

    def verify_challenge(self, params: dict) -> "Optional[Union[str, WebhookResponse]]":
        """GET verification handshake (e.g. Meta hub.challenge). A str answers
        ``text/plain``; a :class:`WebhookResponse` is rendered verbatim. Default:
        no handshake."""
        return None

    def decode_payload(self, payload: dict, headers: Optional[dict]) -> Optional[dict]:
        """The plain event for a parsed body (064 order 0003). Default: unchanged.

        Return ``None`` to REFUSE (bad ciphertext, a wrong verification token):
        the request answers 400 and no turn runs. ``headers`` are the lower-cased
        request headers, or ``None`` on a journal replay (that request was
        verified before its ack)."""
        return payload

    def answer_post(self, payload: dict) -> Optional[WebhookResponse]:
        """An in-band challenge carried in a POST (Feishu ``url_verification``):
        return the answer and no turn runs. Called only after the signature check."""
        return None

    def ack_response(self) -> "Union[dict, WebhookResponse]":
        """What a processed/accepted event answers. Default ``{"ok": true}``."""
        return {"ok": True}

    @abstractmethod
    def parse(self, payload: dict) -> List[InboundMessage]: ...

    @abstractmethod
    def idempotency_key(self, inbound: InboundMessage) -> str: ...

    async def handle_post(self, container, headers: dict, body: bytes,
                          task_agent) -> "Union[dict, WebhookResponse]":
        # 064 F4: the body cap runs BEFORE the signature check (no HMAC over an
        # unbounded body), and the signature BEFORE the JSON parse.
        body = body or b""
        if len(body) > body_cap_for(self.surface_id):
            logger.warning("%s webhook: body of %d bytes over the cap — refused",
                           self.surface_id, len(body))
            return WebhookResponse.json({"ok": False, "error": "body too large"}, status=413)
        # Lower-case header keys so callers can pass platform headers verbatim.
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        if not self.verify_signature(h, body):
            logger.warning("%s webhook: signature verification FAILED", self.surface_id)
            return {"ok": False, "error": "bad signature"}
        try:
            payload = json.loads(body or b"{}")
        except Exception as e:
            logger.warning("%s webhook: bad JSON: %s", self.surface_id, e)
            return {"ok": True}      # ack so the platform stops retrying garbage
        if isinstance(payload, dict):
            payload = self._decode(payload, h)
            if payload is None:
                return WebhookResponse.json({"ok": False, "error": "undecodable payload"},
                                            status=400)
        try:
            answer = self.answer_post(payload) if isinstance(payload, dict) else None
        except Exception as e:
            logger.error("%s webhook: challenge answer failed: %s", self.surface_id, e,
                         exc_info=True)
            answer = None
        if answer is not None:
            return answer
        if self.ack_before_turn and self._journal is not None:
            key = f"{self.surface_id}:{hashlib.sha256(body).hexdigest()}"
            try:
                self._journal.accept(key, self.surface_id, body)
            except Exception as e:
                # Never ack an event we could lose: process inline instead.
                logger.error("%s webhook: journal write failed, processing inline: %s",
                             self.surface_id, e, exc_info=True)
            else:
                # A redelivery can retry an accepted row whose previous drain
                # failed. claim() excludes live drains and exhausted rows.
                self._spawn_drain(container, task_agent, [key])
                return self.ack_response()
        await self._process_payload(container, payload, task_agent)
        return self.ack_response()

    # --- persist-before-ack drain (064 F4) -----------------------------------

    def _spawn_drain(self, container, task_agent, keys: list) -> None:
        from core.async_bridge import spawn_retained
        spawn_retained(self._drain(container, task_agent, keys), self._tasks)

    async def _drain(self, container, task_agent, keys: list) -> int:
        """Run each journaled event's turn once; the number drained."""
        done = 0
        for key in keys:
            body = self._journal.claim(key)
            if body is None:
                continue            # another drain holds it, or it is gone
            try:
                payload = json.loads(body or b"{}")
                if isinstance(payload, dict):
                    payload = self._decode(payload, None)
                if payload is None:
                    # It decoded at accept time; a key rotated since then. It
                    # can never run: finish the row instead of retrying it.
                    logger.error("%s webhook: journaled event %s no longer decodes — dropped",
                                 self.surface_id, key[:12])
                    self._journal.complete(key)
                    continue
                await self._process_payload(container, payload, task_agent, durable=True)
            except asyncio.CancelledError:
                self._journal.release(key)
                raise
            except Exception as e:
                logger.error("%s webhook: drain of %s failed: %s", self.surface_id,
                             key[:12], e, exc_info=True)
                self._journal.release(key)
                continue
            self._journal.complete(key)
            done += 1
        return done

    def _decode(self, payload: dict, headers: Optional[dict]) -> Optional[dict]:
        try:
            out = self.decode_payload(payload, headers)
        except Exception as e:
            # The message only: a decrypt error must not echo the ciphertext.
            logger.warning("%s webhook: payload refused by decode: %s", self.surface_id,
                           type(e).__name__)
            return None
        return out if isinstance(out, dict) else None

    async def recover(self, container, task_agent) -> int:
        """After a restart: drain every event acked but never processed, once.

        ⚠️ Call it BEFORE this surface accepts a request (the gateway does): it
        hands back every ``processing`` row, which is only right for rows a dead
        process held."""
        if self._journal is None:
            return 0
        try:
            keys = self._journal.recover(self.surface_id)
        except Exception as e:
            logger.error("%s webhook: journal unreadable on recover: %s",
                         self.surface_id, e, exc_info=True)
            return 0
        return await self._drain(container, task_agent, keys) if keys else 0

    async def _process_payload(self, container, payload: dict, task_agent, *,
                               durable: bool = False) -> None:
        try:
            messages = self.parse(payload)
        except Exception as e:
            logger.error("%s webhook: parse failed: %s", self.surface_id, e, exc_info=True)
            if durable:
                raise
            return
        for inbound in messages:
            try:
                key = self.idempotency_key(inbound)
                if not durable and key and self._idem.seen(key):
                    continue
                session_key = build_session_key(inbound.identity.source, inbound.identity.user_id)
                async with self._lock.for_key(session_key):
                    # Journaled events mark COMPLETION, not arrival. Claiming
                    # before the await loses the event after a crash/cancellation.
                    # Check under the session lock: retry envelopes can differ
                    # while carrying the same message id.
                    if durable and key and self._idem.peek(key):
                        continue
                    if voice_present(inbound.media):
                        from core.surfaces.media_access import paid_media_allowed
                        if not paid_media_allowed(container, inbound):
                            if durable and key:
                                self._idem.seen(key)
                            continue
                    # Fix 2b: hydrate surface-specific media (e.g. WA media-id -> bytes) before transcription
                    if hasattr(self, "hydrate_media"):
                        try:
                            await self.hydrate_media(inbound.media)
                        except Exception as e:
                            # OB21: a media item that never hydrated reaches the
                            # agent as an empty attachment — say so in the journal.
                            logger.warning("inbound webhook: media hydration failed "
                                           "for %s: %s", type(self).__name__, e,
                                           exc_info=True)
                    # Fix 2a: transcribe voice-only turns; guard untranscribed voice (never route empty)
                    if voice_present(inbound.media) and not (inbound.text or "").strip():
                        try:
                            _vtext = await transcribe_inbound_media(container, inbound.media)
                        except Exception:
                            _vtext = None
                        if _vtext:
                            # CHAT-4: marked as voice, so a transcript that
                            # starts with "/" is never routed as a COMMAND.
                            inbound.text = f"[voice message, auto-transcribed] {_vtext}"
                            for _m in inbound.media:
                                if getattr(_m, "kind", None) in ("voice", "audio"):
                                    _m.transcript = _vtext
                                    break
                    if voice_needs_guard(inbound.media, inbound.text):
                        await self._send_immediate(
                            inbound,
                            voice_unavailable_message(SurfaceConfig.voice_transcription_enabled()),
                        )
                        if durable and key:
                            self._idem.seen(key)
                        continue
                    # Persistent transcript echo (voice only) — see core.surfaces.voice_echo.
                    # Lands before the agent runs; fail-open (never blocks the turn).
                    if SurfaceConfig.voice_transcript_echo_enabled():
                        _t = voice_transcript(inbound.media)
                        if _t:
                            try:
                                await self._send_immediate(
                                    inbound, voice_echo_message(_t),
                                    reply_to=inbound.idempotency_key)
                            except Exception as e:
                                logger.debug("%s transcript echo failed: %s", self.surface_id, e)
                            _mr = getattr(self, "mark_read_inbound", None)
                            if _mr is not None:
                                try:
                                    await _mr(inbound)
                                except Exception:
                                    pass
                    decision = await route_inbound(container, inbound)
                    result = InboundResult(inbound=inbound, decision=decision)

                    # 030 L5: thread deliver= through, like every polling harness
                    # does. Without it, act_on_inbound's failed-run breadcrumb and
                    # LLM-outage notice paths return at their deliver-is-None
                    # guard — a webhook surface (WhatsApp) silently swallowed
                    # exactly the failures those notices were built to surface.
                    async def _deliver(text: str, _inb=inbound, _decision=result.decision) -> None:
                        try:
                            from core.surfaces.command_reply import admit_inbound_reply, reply_text
                            if not admit_inbound_reply(_inb, _decision, text):
                                return
                            await self._send_immediate(_inb, reply_text(text))
                        except Exception:
                            logger.warning("%s deliver failed", self.surface_id,
                                           exc_info=True)

                    reply = await act_on_inbound(task_agent, result, deliver=_deliver)
                    if durable and key:
                        self._idem.seen(key)
                    if reply:
                        # 046 T2: a handler may return a `CommandReply` that
                        # names its destination. A surface with no room model
                        # still renders the TEXT rather than a dataclass repr.
                        await _deliver(reply)
            except Exception as e:  # fail-open per message
                logger.error("%s webhook: process failed: %s", self.surface_id, e, exc_info=True)
                if durable:
                    raise  # keep the acknowledged event for recovery/retry

    async def _send_immediate(self, inbound: InboundMessage, text: str, reply_to=None) -> None:
        """Deliver a synchronous reply (DENIED notice / command ack / transcript echo).
        Override per surface. ``reply_to`` optionally quotes the inbound message."""
        logger.debug("%s immediate reply suppressed (no transport): %s", self.surface_id, text[:80])
