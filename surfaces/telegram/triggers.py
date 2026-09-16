"""Telegram trigger detection shared by the supported chat surfaces.

Server-side MessageEntity values are authoritative; offsets are UTF-16 code
units.
"""
from __future__ import annotations

from typing import Iterable, Optional, Tuple


def entity_text(source: str, offset: int, length: int) -> str:
    if offset < 0 or length <= 0:
        return ""
    try:
        return source.encode("utf-16-le")[offset * 2:(offset + length) * 2].decode("utf-16-le")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return ""


def _entity_sources(msg: dict) -> Iterable[Tuple[str, list]]:
    yield (msg.get("text") or "", msg.get("entities") or [])
    yield (msg.get("caption") or "", msg.get("caption_entities") or [])


def message_mentions_bot(msg: dict, *, bot_username: Optional[str],
                         bot_id: Optional[int]) -> Optional[bool]:
    """True = addressed to OUR bot; False = not; None = identity unknown (the
    dispatcher treats None as not mentioned — fail-closed)."""
    if not bot_username and bot_id is None:
        return None
    expected = f"@{bot_username.lower()}" if bot_username else None
    for source, entities in _entity_sources(msg):
        for ent in entities:
            etype = ent.get("type")
            try:
                offset, length = int(ent.get("offset", 0)), int(ent.get("length", 0))
            except (TypeError, ValueError):
                continue
            if etype == "mention" and expected:
                if entity_text(source, offset, length).strip().lower() == expected:
                    return True
            elif etype == "text_mention":
                user = ent.get("user") or {}
                if bot_id is not None and user.get("id") == bot_id:
                    return True
            elif etype == "bot_command" and expected:
                span = entity_text(source, offset, length).lower()
                if "@" in span and span.split("@", 1)[1] == expected[1:]:
                    return True
    return False


def is_reply_to_bot(msg: dict, *, bot_username: Optional[str], bot_id: Optional[int]) -> bool:
    frm = (msg.get("reply_to_message") or {}).get("from") or {}
    if not frm.get("is_bot"):
        return False
    if bot_id is not None and frm.get("id") == bot_id:
        return True
    return bool(bot_username) and str(frm.get("username") or "").lower() == bot_username.lower()


def sender_is_bot(msg: dict) -> bool:
    return bool((msg.get("from") or {}).get("is_bot"))


GENERAL_TOPIC_THREAD_ID = "1"


def effective_thread_id(msg: dict) -> Optional[str]:
    """Routable forum thread id, or None. Forum General arrives with no
    message_thread_id but Telegram addresses it as 1; a plain group reply carries a
    message_thread_id that is only a reply-UI anchor and must NOT route."""
    chat = msg.get("chat") or {}
    is_forum = bool(chat.get("is_forum"))
    raw = msg.get("message_thread_id")
    if raw is not None and (is_forum or msg.get("is_topic_message")):
        return str(raw)
    return GENERAL_TOPIC_THREAD_ID if is_forum else None
