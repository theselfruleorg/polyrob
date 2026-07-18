"""WhatsApp Cloud API inbound: HMAC-SHA256 webhook signature, hub.challenge handshake, and
payload->InboundMessage parsing. Thin: dedup/route/act all live in WebhookSurface (Phase 3)."""
import hashlib
import hmac
import logging
from typing import List, Optional

from core.surfaces.envelopes import InboundMessage, Identity, SessionSource
from core.surfaces.idempotency import IdempotencyStore
from core.surfaces.inbound_webhook import WebhookSurface
from core.surfaces.media import Media

import surfaces.telegram.harness  # noqa: F401 — registers the shared inbound actor (core.surfaces.act)

logger = logging.getLogger(__name__)


class WhatsAppInbound(WebhookSurface):
    def __init__(self, idempotency: IdempotencyStore, *, user_directory, window=None,
                 media_fetch=None, responder=None, mark_read=None) -> None:
        super().__init__(idempotency)
        self._ud = user_directory
        self._window = window
        self._media_fetch = media_fetch  # async callable(media_id) -> bytes | None
        self._responder = responder      # async callable(wa_phone, text, reply_to=None) -> Any
        self._mark_read = mark_read       # async callable(message_id) -> Any

    async def _send_immediate(self, inbound, text: str, reply_to=None) -> None:
        """Deliver a synchronous reply (voice-guard / DENIED / transcript echo). The reply
        answers a just-received inbound, so the 24h window is open. Fail-open."""
        if self._responder is None:
            logger.debug("whatsapp: no responder wired; immediate reply suppressed: %s", text[:80])
            return
        to = inbound.identity.raw_user_id or inbound.identity.user_id
        try:
            await self._responder(to, text, reply_to=reply_to)
        except Exception as exc:
            logger.warning("whatsapp: immediate reply to %s failed: %s", to, exc)

    async def mark_read_inbound(self, inbound) -> None:
        """Mark the inbound voice message as read (✓✓) — the lightweight 'received' signal
        that pairs with the transcript echo. Fail-open."""
        if self._mark_read is None or not inbound.idempotency_key:
            return
        try:
            await self._mark_read(inbound.idempotency_key)
        except Exception as exc:
            logger.debug("whatsapp: mark_read failed for %s: %s", inbound.idempotency_key, exc)

    async def hydrate_media(self, media_list) -> None:
        """Fetch bytes for voice/audio Media items that carry a media_id (filename) but no data.
        WhatsApp delivers media as an opaque ID; the bytes must be retrieved via the Graph API
        before transcription. Fail-open: a fetch failure leaves .data=None (transcription skips)."""
        if not self._media_fetch or not media_list:
            return
        for m in media_list:
            if getattr(m, "kind", None) in ("voice", "audio") and not m.data and m.filename:
                try:
                    raw = await self._media_fetch(m.filename)
                    if raw:
                        m.data = raw
                except Exception as exc:
                    logger.debug("whatsapp: hydrate_media failed for %s: %s", m.filename, exc)

    @property
    def surface_id(self) -> str:
        return "whatsapp"

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        from agents.task.surface_config import SurfaceConfig
        secret = SurfaceConfig.webhook_secret("whatsapp")
        if not secret:
            logger.warning("whatsapp: no WHATSAPP_WEBHOOK_SECRET set — rejecting")
            return False
        raw = headers.get("x-hub-signature-256") or ""
        if not raw.startswith("sha256="):
            return False
        got = raw[len("sha256="):]
        want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(got, want)

    def verify_challenge(self, params: dict) -> Optional[str]:
        from agents.task.surface_config import SurfaceConfig
        token = SurfaceConfig.webhook_verify_token("whatsapp")
        if token and params.get("hub.verify_token") == token:
            return params.get("hub.challenge")
        return None

    def parse(self, payload: dict) -> List[InboundMessage]:
        out: List[InboundMessage] = []
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                for m in value.get("messages", []) or []:
                    msg = self._one(m)
                    if msg is not None:
                        out.append(msg)
        return out

    def idempotency_key(self, inbound: InboundMessage) -> str:
        return inbound.idempotency_key or ""

    def _one(self, m: dict) -> Optional[InboundMessage]:
        wa_from = str(m.get("from") or "")
        if not wa_from:
            return None
        if self._window is not None:
            import time as _t
            try:
                self._window.touch(wa_from, now=_t.time())
            except Exception:
                pass
        try:
            user_id = self._ud.resolve_internal(wa_from, "whatsapp")
        except Exception:
            user_id = "wa_" + wa_from
        ident = Identity(
            user_id=user_id,
            source=SessionSource(surface_id="whatsapp", chat_id=wa_from, chat_type="dm"),
            raw_user_id=wa_from,
        )
        mtype = m.get("type")
        text, media = "", []
        if mtype == "text":
            text = (m.get("text") or {}).get("body", "")
        elif mtype in ("audio", "voice"):
            media = [Media(kind="voice", mime="audio/ogg",
                           url=None, filename=(m.get(mtype) or {}).get("id"))]
            # NOTE: WhatsApp media is fetched by media-id via the Graph API in the harness
            # (client.download_media); url left None here, filled before transcription.
        elif mtype == "image":
            media = [Media(kind="image", caption=(m.get("image") or {}).get("caption"))]
        return InboundMessage(text=text, identity=ident,
                              idempotency_key=str(m.get("id") or ""), media=media)
