"""The bot's own membership in a room changed (Telegram ``my_chat_member``).

A kicked room is marked ``left`` by the outbound seam when a send fails. Before
this module the poller never asked for ``my_chat_member``, so a re-add was
invisible: prod 2026-11-15 kept a room the owner had re-added at ``left`` and
every room tool refused it. Now:

- in (member / administrator / creator / restricted-but-member) -> a ``left``
  row turns ``active`` again and the dead latch is lifted
  (``core.surfaces.room_keys.restore_rejoined_room``). A revoked or never-allowed
  room is NOT touched: an add is not the owner's permission.
- out (left / kicked) -> the room is marked ``left`` at once, instead of on the
  next failed send.

The owner is told either way, only when a row actually moved. The update is
never a turn.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

_IN = frozenset({"member", "administrator", "creator"})
_OUT = frozenset({"left", "kicked"})


def membership_of(update: dict) -> Optional[Tuple[str, str, Optional[bool]]]:
    """``(chat_id, title, is_in)`` of a ``my_chat_member`` update, else None.
    ``is_in`` is None for a change that is neither a join nor a departure (a
    rights change inside the room). Every field comes from Telegram."""
    mcm = update.get("my_chat_member")
    if not isinstance(mcm, dict):
        return None
    chat = mcm.get("chat") or {}
    if chat.get("id") is None or chat.get("type") == "private":
        return None
    new = mcm.get("new_chat_member") or {}
    status = str(new.get("status") or "")
    if status in _IN or (status == "restricted" and new.get("is_member")):
        is_in: Optional[bool] = True
    elif status in _OUT or (status == "restricted" and new.get("is_member") is False):
        is_in = False
    else:
        is_in = None
    title = str(chat.get("title") or chat.get("username") or "")
    return str(chat["id"]), title, is_in


def _container(harness: Any) -> Any:
    return getattr(harness, "container", None) or getattr(
        getattr(harness, "task_agent", None), "container", None)


async def absorb(harness: Any, update: dict) -> bool:
    """Handle a ``my_chat_member`` update. True = the update stops here."""
    m = membership_of(update)
    if m is None:
        return "my_chat_member" in update  # a private-chat block/unblock: no room
    await on_change(_container(harness), *m)
    return True


async def on_change(container: Any, chat_id: str, title: str, is_in: Optional[bool]) -> bool:
    """Apply one membership change. Never raises. True iff a row moved."""
    if container is None or is_in is None:
        return False
    try:
        from core.surfaces import room_keys
        if is_in:
            moved = room_keys.restore_rejoined_room(container, "telegram", chat_id)
            note = (f"I am back in {title or 'a room'} (telegram {chat_id}). The room "
                    f"was marked LEFT after I was removed; it is active again.")
        else:
            from core.runtime_paths import container_data_home
            moved = room_keys.mark_room_left(container_data_home(container), "telegram", chat_id)
            note = (f"I was removed from {title or 'a room'} (telegram {chat_id}). The room "
                    f"is marked LEFT; add me back and it turns active again by itself.")
        if not moved:
            return False
        logger.warning("telegram room %s %s (my_chat_member)", chat_id,
                       "restored" if is_in else "marked LEFT")
        try:
            from core.instance import resolve_owner_user_id
            from core.surfaces.user_delivery import deliver_user_message
            await deliver_user_message(container, resolve_owner_user_id(), note)
        except Exception as e:
            logger.warning("room membership: the owner note was not sent: %s", e)
        return True
    except Exception:
        logger.warning("room membership change failed for %s", chat_id, exc_info=True)
        return False
