"""Dispatch one bounded Telegram batch, preserving each chat's update order."""
import asyncio
import logging
from collections import deque

logger = logging.getLogger(__name__)
WORKERS = 4


def _chat_key(update):
    callback = update.get("callback_query") or {}
    message = (update.get("message") or update.get("edited_message")
               or update.get("channel_post") or update.get("edited_channel_post")
               or callback.get("message") or {})
    return str((message.get("chat") or {}).get("id")
               or (callback.get("from") or {}).get("id") or "unknown")


async def dispatch_batch(updates, handle_update):
    """At most four chats run together; an update never overtakes its chat.

    The caller waits before fetching the next batch, so Telegram does not
    acknowledge queued updates before they have been handled. No work outlives
    cancellation of the polling task.
    """
    groups = {}
    for update in updates:
        groups.setdefault(_chat_key(update), []).append(update)
    pending = deque(groups.values())

    async def worker():
        while pending:
            for update in pending.popleft():
                try:
                    await handle_update(update)
                except Exception as exc:
                    logger.warning("telegram update handler failed (%s)", type(exc).__name__)

    async with asyncio.TaskGroup() as group:
        for _ in range(min(WORKERS, len(pending))):
            group.create_task(worker())
