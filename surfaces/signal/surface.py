"""SignalSurface — the Signal transport as a Surface contract impl.

Pure envelope parsing (``parse_envelope``) + the outbound surface. Targets
resolve from the session_key chat segment: an E164 number for DMs, a
``group.<id>`` chat id for groups. No edit support; a minimum interval
between sends is enforced (Signal-Server rate limits are aggressive; the
full token-bucket-with-429-feedback is deferred).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from core.surfaces.envelopes import (Identity, InboundMessage, OutboundMessage,
                                     SendResult, SurfaceCapabilities)
from core.surfaces.surface import Surface, split_message

logger = logging.getLogger(__name__)

_SIGNAL_MAX = 2000


def _min_interval() -> float:
    try:
        return float(os.getenv("SIGNAL_SEND_MIN_INTERVAL_SEC", "1.0"))
    except ValueError:
        return 1.0


def target_from_session_key(session_key: str) -> str:
    parts = session_key.split(":")
    if parts and parts[0] == "direct":
        return parts[-1]
    if len(parts) >= 5:
        return parts[4]
    return parts[-1] if parts else session_key


def parse_envelope(envelope: dict, account: str,
                   user_directory: Any = None) -> Optional[InboundMessage]:
    """signal-cli envelope → InboundMessage (dataMessage only), or None.

    Skips our own sync messages, receipts and typing indicators. A group
    message keys the chat as ``group.<groupId>`` with chat_type "group"
    (W3 gating applies); Signal has no @mention concept → ``mentions_bot``
    stays None (mention-gated groups therefore stay silent unless
    GROUP_REQUIRE_MENTION=false).
    """
    if not isinstance(envelope, dict):
        return None
    data = envelope.get("dataMessage")
    if not isinstance(data, dict):
        return None
    sender = str(envelope.get("sourceNumber") or envelope.get("source")
                 or envelope.get("sourceUuid") or "")
    if not sender or sender == account:
        return None
    text = str(data.get("message") or "").strip()
    if not text:
        return None

    group_info = data.get("groupInfo") or {}
    group_id = group_info.get("groupId")
    from core.surfaces.envelopes import SessionSource
    if group_id:
        source = SessionSource(surface_id="signal",
                               chat_id=f"group.{group_id}",
                               chat_type="group")
    else:
        source = SessionSource(surface_id="signal", chat_id=sender,
                               chat_type="dm")

    user_id = None
    try:
        from core.instance import owner_surface_alias
        user_id = owner_surface_alias(sender, "signal")
    except Exception:
        user_id = None
    if not user_id and user_directory is not None:
        try:
            user_id = user_directory.resolve_internal(sender, "signal")
        except Exception:
            user_id = None
    if not user_id:
        user_id = f"u_signal_{sender}"

    return InboundMessage(
        text=text,
        identity=Identity(user_id=user_id, source=source, raw_user_id=sender,
                          display_name=envelope.get("sourceName")),
        idempotency_key=f"{sender}:{envelope.get('timestamp')}",
        raw=envelope,
        mentions_bot=None,
    )


class SignalSurface(Surface):
    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client
        self._last_send = 0.0

    @property
    def surface_id(self) -> str:
        return "signal"

    @property
    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            supports_streaming=False,   # buffered flush only
            supports_edit=False,
            is_multi_tenant=True,
            max_message_bytes=_SIGNAL_MAX,
            markdown_flavor="none",
        )

    async def _throttle(self) -> None:
        wait = _min_interval() - (time.monotonic() - self._last_send)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send = time.monotonic()

    async def send(self, msg: OutboundMessage) -> SendResult:
        target = target_from_session_key(msg.session_key)
        try:
            for chunk in split_message(msg.text or "", _SIGNAL_MAX):
                await self._throttle()
                await self._client.send(target, chunk)
            return SendResult(success=True)
        except Exception as e:  # fail-open
            logger.error("SignalSurface.send to %s failed: %s", target, e)
            return SendResult(success=False, error=str(e))

    async def start(self, container) -> None:
        return None

    async def stop(self) -> None:
        return None
