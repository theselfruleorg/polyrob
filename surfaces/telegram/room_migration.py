"""101: Telegram's own migration service message moves the room (G7)."""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


def migration_of(update: dict) -> Optional[Tuple[str, str]]:
    """``(old_chat_id, new_chat_id)`` of a service message, else None. Both fields come
    from Telegram: no client can set them."""
    msg = update.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    if chat is None:
        return None
    if msg.get("migrate_to_chat_id") is not None:
        return str(chat), str(msg["migrate_to_chat_id"])
    if msg.get("migrate_from_chat_id") is not None:
        return str(msg["migrate_from_chat_id"]), str(chat)
    return None


async def absorb(harness: Any, update: dict) -> bool:
    """A migration message is read first of all: its new id is not on the allowlist yet,
    and an anonymous admin may be the sender. It is never a turn."""
    if migration_of(update) is None:
        return False
    await on_update(getattr(harness, "container", None) or getattr(
        getattr(harness, "task_agent", None), "container", None), update)
    return True


async def on_update(container: Any, update: dict) -> bool:
    """Move the room if this update says so. Never raises: a fault costs the move and
    is said in the log, never the update."""
    pair = migration_of(update)
    if pair is None or container is None:
        return False
    try:
        from core.instance import resolve_owner_user_id
        from core.runtime_paths import container_data_home
        from core.surfaces.room_migrate import migrate_room
        from cron.room_service import migrate_service_jobs
        moved = migrate_room(container, "telegram", *pair)
        if not moved:
            return False
        jobs = migrate_service_jobs(container_data_home(container), "telegram", *pair)
        what = ", ".join(moved + ([f"{jobs} service job(s)"] if jobs else []))
        try:
            from core.surfaces.user_delivery import deliver_user_message
            await deliver_user_message(
                container, resolve_owner_user_id(),
                f"A group became a supergroup, so its chat id changed from {pair[0]} to "
                f"{pair[1]}. I moved: {what}. Its next line starts a fresh session.")
        except Exception as e:
            logger.warning("room migration: the owner note was not sent: %s", e)
        return True
    except Exception:
        logger.warning("room migration failed for %s", pair, exc_info=True)
        return False
