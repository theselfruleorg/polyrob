"""A latch that sets only after success (098 S9).

``run(fn)`` runs ``fn`` at most until it succeeds once: a raise leaves the
latch open, so the next call tries again. ``reset()`` re-opens it (an owner
path). Thread-safe for sync callers, task-safe for async callers.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Callable, Optional


class OnceLatch:
    def __init__(self) -> None:
        self._done = False
        self._lock = threading.Lock()
        self._alock: Optional[asyncio.Lock] = None

    def done(self) -> bool:
        return self._done

    def reset(self) -> None:
        self._done = False

    def run(self, fn: Callable[[], Any]) -> bool:
        """True when this call ran ``fn`` successfully; False when already done.
        A raise from ``fn`` propagates and leaves the latch open."""
        if self._done:
            return False
        with self._lock:
            if self._done:
                return False
            fn()
            self._done = True
            return True

    async def arun(self, coro_fn: Callable[[], Awaitable[Any]]) -> bool:
        if self._done:
            return False
        if self._alock is None:
            self._alock = asyncio.Lock()
        async with self._alock:
            if self._done:
                return False
            await coro_fn()
            self._done = True
            return True
