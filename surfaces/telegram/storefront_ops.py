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
        # 110 W0: this runs BEFORE process_update's dedup (the raw allowlist drops a
        # stranger first), and the poll offset is in memory: a restart replays the
        # update, and an unclaimed `/buy` made a second order. Claim the update here;
        # a redelivery is "handled" (already answered), never re-run.
        dedup = getattr(harness, "dedup", None)
        update_id = update.get("update_id")
        if dedup is not None and update_id is not None and dedup.seen(update_id):
            logger.debug("telegram storefront: dropping redelivered update %s", update_id)
            return True
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


def _is_correspondent(harness: Any, tg_id: Any) -> bool:
    """113 W0: a person the agent wrote to on Telegram answers in a DM. The raw DM
    lock dropped that answer before routing, so the agent waited for a reply nobody
    could deliver. True only while the tier model is ON: with it off the dispatcher
    has no closed tier table, and a sender let through here would reach the owner
    path. Fail CLOSED: any fault answers False (the sender stays a stranger)."""
    try:
        from core.surfaces.config import SurfaceConfig
        if not SurfaceConfig.correspondent_access_enabled():
            return False
        container = getattr(harness, "container", None)
        registry = container.get_service("correspondent_registry") if container else None
        if registry is None:
            return False
        return registry.resolve(surface="telegram", address=str(tg_id)) is not None
    except Exception:
        logger.warning("telegram correspondent probe failed (the sender is ignored)",
                       exc_info=True)
        return False


async def _stranger_door(harness: Any, update: dict, tg_id: Any) -> str:
    """A private-chat sender who is NOT on the Telegram allowlist (the DM lock).

    ``"paired"``: pairing is on and the owner paired them — route them (the dispatcher
    gives them the owner tier under their own tenant). ``"answered"``: a storefront line
    was answered, or a pairing code was sent (pairing on). ``"raw_allowlist"``: ignore.
    ``"correspondent"``: the agent wrote to this person first and the tier model is
    on — route them (the dispatcher delivers the line as DATA to the session that
    wrote; never a turn, never a verb). The paired and correspondent checks run before
    the storefront claims the update in dedup; nothing is downloaded or transcribed
    for a stranger."""
    from core.pairing import code_reply, guard_inbound, pairing_required
    uid = None
    if pairing_required():
        try:
            from tools.user_directory import UserDirectory
            uid = UserDirectory._derive_internal_id("telegram", str(tg_id))
            from core.pairing import store_for
            cfg = getattr(getattr(harness, "container", None), "config", None)
            if store_for(getattr(cfg, "data_dir", None)).is_paired(uid):
                return "paired"
        except Exception:
            logger.warning("telegram pairing check failed (the sender is ignored)",
                           exc_info=True)
            return "raw_allowlist"
    if _is_correspondent(harness, tg_id):
        return "correspondent"
    if await _storefront(harness, update):
        return "answered"
    msg = update.get("message") or {}
    if uid is None or (msg.get("from") or {}).get("is_bot"):
        return "raw_allowlist"
    decision = guard_inbound(getattr(harness, "container", None), uid,
                             surface_id="telegram", raw_id=str(tg_id))
    if decision is None or not decision.pairing_code:
        return "raw_allowlist"          # told within 10 minutes, or the store failed
    try:
        await harness.bot.send_message((msg.get("chat") or {}).get("id"),
                                       code_reply("telegram", decision.pairing_code))
    except Exception:
        logger.warning("telegram pairing code not sent", exc_info=True)
    return "answered"
