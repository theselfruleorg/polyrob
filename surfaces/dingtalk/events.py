"""DingTalk bot-message callback (Stream topic ``/v1.0/im/bot/messages/get``)
→ InboundMessage. Pure (tested offline).

Callback data (the frame's ``data`` JSON): ``conversationId``,
``conversationType`` (``"1"`` single chat · ``"2"`` group), ``senderStaffId``
(the platform-authenticated staff id of an org member — preferred) else
``senderId``, ``senderNick``, ``chatbotUserId`` (the bot's own id), ``msgId``,
``msgtype``, ``text.content``, ``sessionWebhook`` +
``sessionWebhookExpiredTime`` (ms epoch), ``isInAtList``.

Rules:
- only ``text`` messages in this order;
- the bot's own messages are dropped;
- a single chat → dm; a group message is read ONLY when ``isInAtList`` is true
  (fail closed: a missing flag means no group message is read);
- owner recognition stays pairing-row only (the catalog row has no alias).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.surfaces.envelopes import Identity, InboundMessage, SessionSource

logger = logging.getLogger(__name__)

TOPIC = "/v1.0/im/bot/messages/get"


def sender_id(data: dict) -> str:
    return str(data.get("senderStaffId") or data.get("senderId") or "")


def is_group(data: dict) -> bool:
    return str(data.get("conversationType") or "") == "2"


def webhook_expiry_s(data: dict) -> float:
    """``sessionWebhookExpiredTime`` (ms epoch) → epoch seconds; 0 = unknown."""
    try:
        return int(data.get("sessionWebhookExpiredTime") or 0) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def parse_message(data: Any, user_directory: Any = None) -> Optional[InboundMessage]:
    if not isinstance(data, dict):
        return None
    if data.get("msgtype") != "text":
        return None
    author = sender_id(data)
    if not author:
        return None
    bot = str(data.get("chatbotUserId") or "")
    if bot and (author == bot or str(data.get("senderId") or "") == bot):
        return None
    text_obj = data.get("text") or {}
    text = text_obj.get("content") if isinstance(text_obj, dict) else None
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    if not text:
        return None

    group = is_group(data)
    if group and data.get("isInAtList") is not True:
        return None

    chat_id = str(data.get("conversationId") or "")
    source = SessionSource(surface_id="dingtalk", chat_id=chat_id,
                           chat_type="group" if group else "dm")
    user_id = None
    if user_directory is not None:
        try:
            user_id = user_directory.resolve_internal(author, "dingtalk")
        except Exception:
            logger.debug("dingtalk: user directory resolve failed", exc_info=True)
            user_id = None
    if not user_id:
        user_id = f"u_dingtalk_{author}"

    return InboundMessage(
        text=text,
        identity=Identity(user_id=user_id, source=source, raw_user_id=author,
                          display_name=str(data.get("senderNick") or "") or None),
        idempotency_key=str(data.get("msgId") or "") or None,
        raw=data,
        mentions_bot=bool(group),
    )

