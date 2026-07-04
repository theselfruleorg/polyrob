"""Per-key async serialization so one chat's messages process in order while different
chats stay concurrent. Locks are created lazily and reaped when uncontended."""
import asyncio
from contextlib import asynccontextmanager
from typing import Dict


class KeyedLock:
    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def for_key(self, key: str):
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            if not lock.locked() and not lock._waiters:  # reap uncontended
                self._locks.pop(key, None)
