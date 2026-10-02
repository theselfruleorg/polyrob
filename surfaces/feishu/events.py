"""Feishu ``im.message.receive_v1`` → InboundMessage. Pure (tested offline).

Event shape (schema 2.0): ``header.event_type``; ``event.sender.sender_id.open_id``
(the app-scoped user id the platform authenticates) and ``sender_type``;
``event.message`` with ``message_id``, ``chat_id``, ``chat_type`` (``p2p`` |
``group``), ``message_type``, ``content`` (a JSON STRING) and ``mentions``
(``[{key: "@_user_1", id: {open_id}, name}]`` — the text carries the key).

Rules:
- only a human sender (``sender_type == "user"``), never the bot itself;
- ``text`` messages, and (order 0004) ``image`` / ``file`` / ``audio`` / ``media``
  messages as :class:`Media` with ``ref = "<message_id>:<file_key>"`` — the bytes
  are fetched later by the harness, and only for a message that routes;
- ``p2p`` → dm; a group message is read ONLY when the bot's own open_id is one of
  its mentions (fail closed: an unknown bot id means no group message is read);
- mention keys are rendered as ``@name``; the bot's own mention is removed.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.media import Media

logger = logging.getLogger(__name__)

EVENT_TYPE = "im.message.receive_v1"


def _text_of(message: dict) -> Optional[str]:
    if message.get("message_type") != "text":
        return None
    try:
        content = json.loads(message.get("content") or "")
    except (TypeError, ValueError):
        return None
    text = content.get("text") if isinstance(content, dict) else None
    return text if isinstance(text, str) else None


#: message_type → (Media.kind, the content key holding the file key).
_MEDIA_TYPES = {"image": ("image", "image_key"), "file": ("document", "file_key"),
                "audio": ("audio", "file_key"), "media": ("video", "file_key")}


def _media_of(message: dict) -> list:
    spec = _MEDIA_TYPES.get(message.get("message_type"))
    if spec is None:
        return []
    try:
        content = json.loads(message.get("content") or "")
    except (TypeError, ValueError):
        return []
    if not isinstance(content, dict):
        return []
    key = str(content.get(spec[1]) or "")
    mid = str(message.get("message_id") or "")
    if not key or not mid or ":" in key or ":" in mid:
        return []
    return [Media(kind=spec[0], ref=f"{mid}:{key}",
                  filename=str(content.get("file_name") or "") or None)]


def parse_event(payload: dict, bot_open_id: str,
                user_directory: Any = None) -> Optional[InboundMessage]:
    if not isinstance(payload, dict):
        return None
    header = payload.get("header") or {}
    if not isinstance(header, dict) or header.get("event_type") != EVENT_TYPE:
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    sender = event.get("sender") or {}
    message = event.get("message") or {}
    if not isinstance(sender, dict) or not isinstance(message, dict):
        return None
    if sender.get("sender_type") != "user":
        return None
    author = str((sender.get("sender_id") or {}).get("open_id") or "")
    if not author or (bot_open_id and author == bot_open_id):
        return None

    text = _text_of(message)
    media = []
    if text is None:
        media = _media_of(message)
        if not media:
            return None
        text = ""

    mentioned = False
    rendered = {}
    for m in message.get("mentions") or []:
        if not isinstance(m, dict):
            continue
        key = str(m.get("key") or "")
        mid = str((m.get("id") or {}).get("open_id") or "")
        if bot_open_id and mid == bot_open_id:
            mentioned = True
            rendered[key] = ""
        else:
            rendered[key] = f"@{m.get('name') or 'user'}"
    rendered.pop("", None)
    if rendered:
        # ONE pass, longest key first ("@_user_1" is a prefix of "@_user_10"),
        # so a rendered name is never itself rewritten by a later key.
        keys = sorted(rendered, key=len, reverse=True)
        text = re.sub("|".join(map(re.escape, keys)), lambda mt: rendered[mt.group(0)], text)
    text = " ".join(text.split())

    is_dm = message.get("chat_type") == "p2p"
    if not is_dm and not mentioned:
        return None
    if not text and not media:
        return None

    chat_id = str(message.get("chat_id") or "")
    source = SessionSource(surface_id="feishu", chat_id=chat_id,
                           chat_type="dm" if is_dm else "group")
    user_id = None
    if user_directory is not None:
        try:
            user_id = user_directory.resolve_internal(author, "feishu")
        except Exception:
            logger.debug("feishu: user directory resolve failed", exc_info=True)
            user_id = None
    if not user_id:
        user_id = f"u_feishu_{author}"

    message_id = str(message.get("message_id") or "")
    return InboundMessage(
        text=text,
        identity=Identity(user_id=user_id, source=source, raw_user_id=author),
        idempotency_key=message_id or str(header.get("event_id") or "") or None,
        raw=payload,
        mentions_bot=mentioned,
        media=media,
    )
