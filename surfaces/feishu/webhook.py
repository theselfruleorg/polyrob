"""Feishu / Lark webhook fallback (064 order 0003): the event-subscription
"request URL" mode, for an app whose long connection is not available.

The WS long connection (``ws.py``) stays the default transport. This mode needs
no SDK (bare install): ``FEISHU_TRANSPORT=webhook`` and a public
``/webhooks/feishu`` route (order 0006).

Authentication, from the platform's own model:

* **Encrypt Key** (``FEISHU_ENCRYPT_KEY``) — the body is ``{"encrypt": "<b64>"}``,
  AES-256-CBC with ``key = sha256(encrypt_key)``, the IV the first 16 bytes of
  the decoded blob, PKCS#7 padding. Every EVENT request is signed:
  ``X-Lark-Signature = sha256(timestamp + nonce + encrypt_key + raw_body)``.
  The ``url_verification`` challenge carries no signature, so a missing
  signature is accepted ONLY for that one request type (after it decrypts —
  which already proves the key).
* **Verification Token** (``FEISHU_VERIFICATION_TOKEN``) — the ``token`` field
  inside the (decrypted) body, v1 top-level or schema-2.0 ``header.token``.

Neither configured = every request refused (fail closed): an unauthenticated
webhook would let anyone type as any open_id. With an Encrypt Key set, a
plaintext body is refused too. A Verification Token ALONE carries no signature
and no timestamp — a static secret in every body — so token-only mode is
refused too unless the operator opts in with ``FEISHU_WEBHOOK_ALLOW_UNSIGNED``.

Decryption runs ONCE, in :meth:`decode_payload` (the base class's hook), so
``answer_post`` (the challenge) and ``parse`` (the event) read the plain event.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Awaitable, Callable, List, Optional

from core.surfaces.envelopes import InboundMessage
from core.surfaces.inbound_webhook import WebhookResponse, WebhookSurface

logger = logging.getLogger(__name__)


def decrypt(encrypt_key: str, blob: str) -> dict:
    """The plain JSON event inside a Feishu ``encrypt`` field. Raises on any
    malformed input (bad base64, bad length, bad padding, not JSON)."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = base64.b64decode(blob, validate=True)
    if len(raw) < 32 or len(raw) % 16:
        raise ValueError("ciphertext has a bad length")
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    dec = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
    padded = dec.update(raw[16:]) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    out = json.loads(plain.decode("utf-8"))
    if not isinstance(out, dict):
        raise ValueError("decrypted event is not an object")
    return out


def encrypt(encrypt_key: str, event: dict, iv: bytes) -> str:
    """The inverse of :func:`decrypt` — for recorded test fixtures only."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    padder = padding.PKCS7(128).padder()
    data = padder.update(json.dumps(event).encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + enc.update(data) + enc.finalize()).decode("ascii")


#: How far (seconds, either direction) a signed request's
#: ``X-Lark-Request-Timestamp`` may sit from our clock. The signature binds the
#: timestamp, so outside this window a captured signed event is refused; inside
#: it the message-id dedup (``DEDUP_WINDOW_S``, wider) absorbs a replay.
REPLAY_WINDOW_S = 300
DEDUP_WINDOW_S = 2 * REPLAY_WINDOW_S


def fresh_timestamp(raw: str, *, now: Optional[float] = None) -> bool:
    """True if ``raw`` is integer epoch SECONDS within :data:`REPLAY_WINDOW_S`."""
    try:
        ts = int(str(raw).strip())
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    return abs(now - ts) <= REPLAY_WINDOW_S


UNSIGNED_OPT_IN = "FEISHU_WEBHOOK_ALLOW_UNSIGNED"


def webhook_auth_gap(encrypt_key: str, token: str, allow_unsigned: bool) -> Optional[str]:
    """Why webhook mode cannot authenticate an event, naming the missing key;
    None when it can. The ONE rule ``launch``, the probe and the hook share."""
    if encrypt_key:
        return None
    if not token:
        return ("FEISHU_TRANSPORT=webhook needs FEISHU_ENCRYPT_KEY (or "
                "FEISHU_VERIFICATION_TOKEN) — an unauthenticated webhook is refused")
    if allow_unsigned:
        return None
    return ("FEISHU_ENCRYPT_KEY is not set: with only FEISHU_VERIFICATION_TOKEN no event "
            "is signed, so anyone holding the token or one captured body can type as any "
            f"user. Set FEISHU_ENCRYPT_KEY, or {UNSIGNED_OPT_IN}=true to accept unsigned events")


def signature(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    return hashlib.sha256((timestamp + nonce + encrypt_key).encode("utf-8") + body).hexdigest()


class FeishuWebhook(WebhookSurface):
    def __init__(self, idempotency, *, encrypt_key: str = "",
                 verification_token: str = "", bot_open_id: str = "",
                 user_directory: Any = None,
                 send: Optional[Callable[[str, str], Awaitable[Any]]] = None,
                 journal=None, allow_unsigned: bool = False) -> None:
        super().__init__(idempotency, journal=journal)
        self._encrypt_key = encrypt_key or ""
        self._token = verification_token or ""
        self._allow_unsigned = bool(allow_unsigned)
        self.bot_open_id = bot_open_id or ""
        self._user_directory = user_directory
        self._send = send
        self.chat_types: dict = {}

    @property
    def surface_id(self) -> str:
        return "feishu"

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        if webhook_auth_gap(self._encrypt_key, self._token, self._allow_unsigned):
            return False                      # nothing signed to authenticate with: refuse
        sig = str(headers.get("x-lark-signature") or "")
        if not sig:
            # Unsigned: only the challenge may be (checked in decode_payload),
            # and only a body that decrypts / carries the token gets that far.
            return True
        if not self._encrypt_key:
            return False                      # a signature we cannot check
        timestamp = str(headers.get("x-lark-request-timestamp") or "")
        expected = signature(timestamp,
                             str(headers.get("x-lark-request-nonce") or ""),
                             self._encrypt_key, body or b"")
        if not hmac.compare_digest(sig, expected):
            return False
        # A valid signature on a stale timestamp is a replayed capture: the
        # 5-minute message-id dedup would otherwise re-run it as a new turn.
        return fresh_timestamp(timestamp)

    def decode_payload(self, payload: dict, headers: Optional[dict]) -> Optional[dict]:
        if "encrypt" in payload:
            if not self._encrypt_key:
                return None
            payload = decrypt(self._encrypt_key, str(payload.get("encrypt") or ""))
        elif self._encrypt_key:
            return None                       # encryption is on: plaintext refused
        if self._token:
            header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
            token = str(payload.get("token") or header.get("token") or "")
            if not hmac.compare_digest(token, self._token):
                return None
        is_challenge = payload.get("type") == "url_verification"
        if (self._encrypt_key and headers is not None and not is_challenge
                and not headers.get("x-lark-signature")):
            return None                       # an event must be signed
        return payload

    def answer_post(self, payload: dict) -> Optional[WebhookResponse]:
        if payload.get("type") == "url_verification":
            return WebhookResponse.json({"challenge": str(payload.get("challenge") or "")})
        return None

    def parse(self, payload: dict) -> List[InboundMessage]:
        from surfaces.feishu.harness import parse_inbound
        inbound = parse_inbound(payload, self.bot_open_id, self._user_directory,
                                self.chat_types)
        return [inbound] if inbound is not None else []

    def idempotency_key(self, inbound: InboundMessage) -> str:
        return f"feishu:{inbound.idempotency_key}" if inbound.idempotency_key else ""

    async def _send_immediate(self, inbound: InboundMessage, text: str, reply_to=None) -> None:
        if self._send is None:
            return
        await self._send(inbound.identity.source.chat_id, text)
