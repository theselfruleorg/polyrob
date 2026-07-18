"""WebhookSurface: the shared inbound contract for push-based platforms (WhatsApp,
BlueBubbles, Telegram-webhook). Owns: signature verify -> fast-200 ack -> parse -> dedup ->
route -> act. A concrete surface implements only verify/parse/idempotency_key. Fail-open
end to end: a parse/route fault is logged, never raised to the HTTP layer (so the platform
gets its 200 and does not retry-storm)."""
import json
import logging
from abc import ABC, abstractmethod
from typing import List, Optional

from core.surfaces.envelopes import InboundMessage
from core.surfaces.idempotency import IdempotencyStore
from core.surfaces.dispatcher import route_inbound
from core.surfaces.serialize import KeyedLock
from core.surfaces.session_chat_registry import build_session_key
from core.surfaces.transcription import voice_present, transcribe_inbound_media
from core.surfaces.voice_guard import voice_needs_guard, voice_unavailable_message
from core.surfaces.voice_echo import voice_transcript, voice_echo_message
from agents.task.surface_config import SurfaceConfig
from core.surfaces.act import InboundResult, act_on_inbound  # actor registered by surfaces.telegram.harness

logger = logging.getLogger(__name__)


class WebhookSurface(ABC):
    def __init__(self, idempotency: IdempotencyStore) -> None:
        self._idem = idempotency
        self._lock = KeyedLock()

    @property
    @abstractmethod
    def surface_id(self) -> str: ...

    @abstractmethod
    def verify_signature(self, headers: dict, body: bytes) -> bool: ...

    def verify_challenge(self, params: dict) -> Optional[str]:
        """GET verification handshake (e.g. Meta hub.challenge). Default: no handshake."""
        return None

    @abstractmethod
    def parse(self, payload: dict) -> List[InboundMessage]: ...

    @abstractmethod
    def idempotency_key(self, inbound: InboundMessage) -> str: ...

    async def handle_post(self, container, headers: dict, body: bytes, task_agent) -> dict:
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
        try:
            messages = self.parse(payload)
        except Exception as e:
            logger.error("%s webhook: parse failed: %s", self.surface_id, e, exc_info=True)
            return {"ok": True}
        for inbound in messages:
            try:
                key = self.idempotency_key(inbound)
                if key and self._idem.seen(key):
                    continue
                session_key = build_session_key(inbound.identity.source, inbound.identity.user_id)
                async with self._lock.for_key(session_key):
                    # Fix 2b: hydrate surface-specific media (e.g. WA media-id -> bytes) before transcription
                    if hasattr(self, "hydrate_media"):
                        try:
                            await self.hydrate_media(inbound.media)
                        except Exception:
                            pass
                    # Fix 2a: transcribe voice-only turns; guard untranscribed voice (never route empty)
                    if voice_present(inbound.media) and not (inbound.text or "").strip():
                        try:
                            _vtext = await transcribe_inbound_media(container, inbound.media)
                        except Exception:
                            _vtext = None
                        if _vtext:
                            inbound.text = _vtext
                            for _m in inbound.media:
                                if getattr(_m, "kind", None) in ("voice", "audio"):
                                    _m.transcript = _vtext
                                    break
                    if voice_needs_guard(inbound.media, inbound.text):
                        await self._send_immediate(
                            inbound,
                            voice_unavailable_message(SurfaceConfig.voice_transcription_enabled()),
                        )
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
                    reply = await act_on_inbound(task_agent, result)
                    if reply:
                        await self._send_immediate(inbound, reply)
            except Exception as e:  # fail-open per message
                logger.error("%s webhook: process failed: %s", self.surface_id, e, exc_info=True)
        return {"ok": True}

    async def _send_immediate(self, inbound: InboundMessage, text: str, reply_to=None) -> None:
        """Deliver a synchronous reply (DENIED notice / command ack / transcript echo).
        Override per surface. ``reply_to`` optionally quotes the inbound message."""
        logger.debug("%s immediate reply suppressed (no transport): %s", self.surface_id, text[:80])
