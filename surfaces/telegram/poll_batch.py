"""Dispatch one bounded Telegram batch, preserving each chat's update order."""
import asyncio
import logging
from collections import deque

logger = logging.getLogger(__name__)
WORKERS = 4
#: A chat whose batch declares a file over this size leaves the batch: its download
#: must not hold back the next getUpdates for every other chat.
LARGE_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_BACKGROUND_CHATS = 4
_FILE_FIELDS = ("document", "video", "animation", "video_note")


def _chat_key(update):
    callback = update.get("callback_query") or {}
    message = (update.get("message") or update.get("edited_message")
               or update.get("channel_post") or update.get("edited_channel_post")
               or callback.get("message") or {})
    return str((message.get("chat") or {}).get("id")
               or (callback.get("from") or {}).get("id") or "unknown")


def _is_large(update):
    message = (update.get("message") or update.get("edited_message")
               or update.get("channel_post") or update.get("edited_channel_post") or {})
    return any(isinstance(message.get(field), dict)
               and isinstance(message[field].get("file_size"), int)
               and message[field]["file_size"] > LARGE_ATTACHMENT_BYTES
               for field in _FILE_FIELDS)


async def _handle_rows(rows, handle_update):
    for update in rows:
        try:
            await handle_update(update)
        except Exception as exc:
            logger.warning("telegram update handler failed (%s)", type(exc).__name__)


class BackgroundLanes:
    """Chats that wait on a large download while polling continues.

    ⚠️ Telegram has acknowledged these updates, so a stop during the download loses
    them; :meth:`close` logs each chat that loses one. A later update of the chat
    runs behind its lane, so it never overtakes."""

    def __init__(self):
        self._tasks = {}
        #: Rows waiting behind a chat's running lane. ONE task per chat drains them,
        #: so a chat that keeps writing during a slow download never grows a chain.
        self._queued = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()

    def holds(self, key):
        return key in self._tasks

    def has_room(self):
        return len(self._tasks) < MAX_BACKGROUND_CHATS

    def start(self, key, rows, handle_update):
        if key in self._queued:          # the lane runs: it handles these after its own
            self._queued[key].extend(rows)
            return
        self._queued[key] = list(rows)

        async def run():
            try:
                while self._queued.get(key):
                    batch, self._queued[key] = self._queued[key], []
                    await _handle_rows(batch, handle_update)
            finally:
                self._queued.pop(key, None)

        task = asyncio.create_task(run())
        self._tasks[key] = task

        def forget(done):
            if self._tasks.get(key) is done:
                del self._tasks[key]

        task.add_done_callback(forget)

    async def close(self):
        """Cancel every lane: no work outlives the polling task."""
        tasks = dict(self._tasks)
        for key, task in tasks.items():
            logger.warning("telegram: polling stopped during a large download in chat %s; "
                           "that message is lost and must be sent again", key)
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)


async def dispatch_batch(updates, handle_update, lanes=None):
    """At most four chats run together; an update never overtakes its chat.

    The caller waits before fetching the next batch, so Telegram does not
    acknowledge queued updates before they have been handled. The exception is a
    chat handed to ``lanes``. No work outlives cancellation of the polling task.
    """
    groups = {}
    for update in updates:
        groups.setdefault(_chat_key(update), []).append(update)
    pending = deque()
    for key, rows in groups.items():
        if lanes is not None and (lanes.holds(key) or (
                lanes.has_room() and any(_is_large(row) for row in rows))):
            lanes.start(key, rows, handle_update)
        else:
            pending.append(rows)

    async def worker():
        while pending:
            await _handle_rows(pending.popleft(), handle_update)

    async with asyncio.TaskGroup() as group:
        for _ in range(min(WORKERS, len(pending))):
            group.create_task(worker())
