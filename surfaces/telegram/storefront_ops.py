"""099 W3: the Telegram stranger path of the storefront (``modules.commerce.storefront``).

A sender who is not on the owner allowlist is dropped before routing. Before that
drop, a PRIVATE-chat storefront line (``/start``, ``/offers``, ``/buy``,
``/order``) is answered by code while a live offer lists ``native:telegram``.
Nothing else changes: no session, no model, no tier is granted. Groups keep
their own room tiers (the storefront is DM-only on Telegram in v1).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _storefront(harness: Any, update: dict) -> bool:
    """True when the update was a storefront line and was answered."""
    try:
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private" or chat.get("id") is None:
            return False
        from modules.commerce.storefront import storefront_reply
        reply = await storefront_reply(getattr(harness, "container", None), "telegram",
                                       str(chat["id"]), str(msg.get("text") or ""))
        if reply is None:
            return False
        await harness.bot.send_message(chat["id"], reply)
        return True
    except Exception:
        logger.warning("telegram storefront failed (the line is dropped)", exc_info=True)
        return False
