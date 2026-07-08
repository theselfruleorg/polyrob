"""Centralized sync->async bridge (roadmap P4, Reference §24).

POLYROB had an ad-hoc "detect running loop, spawn a fresh-loop thread, tear it down"
dance inlined in ``Agent._create_llm_from_config`` — run on every agent creation.
That churned a thread + event loop per call and risked httpx/AsyncOpenAI clients
raising "Event loop is closed" when GC'd after their throwaway loop died.

This module provides ONE place for the bridge, backed by a single persistent
background loop thread that is created once and reused. Async clients constructed
on it stay bound to a live loop for the process lifetime.

- No running loop on the caller's thread -> ``asyncio.run`` (cheap, common in CLI).
- A loop IS running (sync-called-from-async, e.g. ``Agent.__init__`` inside the
  async ``create_agent``) -> dispatch to the persistent bridge loop and block on
  the result.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine, Optional


class _PersistentLoop:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever,
                    name="rob-async-bridge",
                    daemon=True,
                )
                self._thread.start()
            return self._loop

    def run(self, coro: "Coroutine[Any, Any, Any]", timeout: Optional[float]) -> Any:
        loop = self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)


_bridge = _PersistentLoop()


def run_coroutine_sync(coro: "Coroutine[Any, Any, Any]", timeout: Optional[float] = 60.0) -> Any:
    """Run ``coro`` to completion from synchronous code, safe under a running loop.

    Returns the coroutine's result; re-raises its exception. ``timeout`` applies
    only to the running-loop (bridge) path.
    """
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False

    if not running:
        # P2-7: on a WORKER thread (e.g. asyncio.to_thread — the reflection/aux offload)
        # there is no running loop, but asyncio.run() spins a THROWAWAY loop PER CALL.
        # An LLM client's httpx pool bound to a prior throwaway loop then raises
        # "Event loop is closed" on the next call — the exact bug this bridge exists to
        # kill. Route worker-thread calls through the persistent bridge loop so every
        # invocation shares one live loop. The MAIN thread with no running loop is the
        # cheap CLI case -> keep asyncio.run.
        if threading.current_thread() is not threading.main_thread():
            return _bridge.run(coro, timeout=timeout)
        if timeout is not None:
            return asyncio.run(asyncio.wait_for(coro, timeout))
        return asyncio.run(coro)
    return _bridge.run(coro, timeout=timeout)
