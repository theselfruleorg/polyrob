"""XSurface — X (Twitter) DMs as a Surface contract impl.

send() resolves the DM participant from the session_key chat segment and posts
via ``POST /2/dm_conversations/with/:participant_id/messages``. X DMs allow
10,000 characters per message (docs.x.com manage-DM reference — NOT the 280
post cap); longer replies split. No edit support. Sends are fail-open.

Budget note: DM sends are 15/15 min + 1,440/24 h per authenticated user, so a
reply that splits into many chunks eats the window — keep replies tight.
"""
from __future__ import annotations

import logging
from typing import Any

from core.surfaces.envelopes import (OutboundMessage, SendResult,
                                     SurfaceCapabilities)
from core.surfaces.surface import Surface, split_message

logger = logging.getLogger(__name__)

_X_DM_MAX = 10000


def participant_id_from_session_key(session_key: str) -> str:
    """Chat-scoped keys: ``agent:main:x:dm:{participant}[...]`` → chat segment
    (index 4); ``direct:x:{participant}`` → last; else last segment."""
    parts = session_key.split(":")
    if parts and parts[0] == "direct":
        return parts[-1]
    if len(parts) >= 5:
        return parts[4]
    return parts[-1] if parts else session_key


class XSurface(Surface):
    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    @property
    def surface_id(self) -> str:
        return "x"

    @property
    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            supports_streaming=False,   # DMs can't edit → buffered flush only
            supports_edit=False,
            is_multi_tenant=True,
            max_message_bytes=_X_DM_MAX,
            markdown_flavor="none",     # X DMs render plain text
        )

    async def send(self, msg: OutboundMessage) -> SendResult:
        participant_id = participant_id_from_session_key(msg.session_key)
        last_event = None
        try:
            for chunk in split_message(msg.text or "", _X_DM_MAX):
                sent = await self._client.send_dm(participant_id, chunk)
                last_event = (sent or {}).get("dm_event_id")
            return SendResult(success=True,
                              surface_message_id=str(last_event) if last_event else None)
        except Exception as e:  # fail-open: never raise into the loop
            logger.error("XSurface.send to %s failed: %s", participant_id, e)
            return SendResult(success=False, error=str(e))

    async def start(self, container) -> None:
        return None

    async def stop(self) -> None:
        return None
