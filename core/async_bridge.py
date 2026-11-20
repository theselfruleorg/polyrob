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
import contextvars
import functools
import inspect
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


_REAP_WAIT_S = 5.0


async def communicate_or_kill(proc: Any, timeout: Optional[float], input: Optional[bytes] = None):
    """``proc.communicate(input)`` within ``timeout``: ``(stdout, stderr)``.

    ``wait_for`` cancels the read, not the OS process. On a timeout or a cancel the child
    is killed and reaped, then the exception goes on. The reap has a bound: a grandchild
    can hold the pipes after the child is gone.
    """
    try:
        return await asyncio.wait_for(proc.communicate(input), timeout)
    except BaseException:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), _REAP_WAIT_S)
        except asyncio.TimeoutError:
            pass
        raise


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
                    name="polyrob-async-bridge",
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


#: The registry of a spawn whose caller never drains its tasks on shutdown.
_RETAINED: set = set()


def spawn_retained(coro, registry: Optional[set] = None):
    """Schedule *coro* fire-and-forget WITH a strong reference held in
    *registry* until it finishes, so the loop cannot GC it mid-run. The
    task-set idiom three session spawners (the HTTP API, A2A, the TaskAgent
    support mixin) each carried by hand; each keeps its own registry so it
    can still drain it on shutdown. No *registry*: the module's own set (a
    one-off spawn nobody drains). Needs a running loop, as create_task does."""
    task = asyncio.create_task(coro)
    registry = _RETAINED if registry is None else registry
    registry.add(task)
    task.add_done_callback(registry.discard)
    return task


# --- Blocking store calls from async code (proposal 125 S15) -------------------
#
# A SQLite store (goals.db, cron, memory) is synchronous; a call on the event loop
# holds every session while the file is contended (up to 17 s in execute_retry).
# The store pool is its own: a contended goals.db must not fill the default
# executor that asyncio.to_thread and the LLM clients share.

_STORE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="store")


async def run_off_loop(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Await ``fn(*args, **kwargs)`` in the store pool, with the caller's contextvars.

    A cancel of the await does NOT stop the call in the thread; a terminal write
    wraps this in :func:`shielded`.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(
        _STORE_POOL, functools.partial(ctx.run, fn, *args, **kwargs))


def offloaded(fn: Callable[..., Any]) -> Callable[..., Coroutine[Any, Any, Any]]:
    """A sync method body becomes an awaitable method that runs in the store pool.

    ``inspect.iscoroutinefunction`` stays True for the decorated method.
    """
    @functools.wraps(fn)
    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        return await run_off_loop(fn, *args, **kwargs)
    return _wrapper


class _OffLoop:
    """The proxy :func:`off_loop` returns: each callable attribute is awaitable."""

    __slots__ = ("_target", "_shield")

    def __init__(self, target: Any, shield: bool = False) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_shield", shield)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr
        shield = self._shield
        if inspect.iscoroutinefunction(attr):
            # Already async (a test double or an @offloaded method): await it as is.
            if not shield:
                return attr

            async def _acall(*args: Any, **kwargs: Any) -> Any:
                return await shielded(attr(*args, **kwargs))
            return _acall

        async def _call(*args: Any, **kwargs: Any) -> Any:
            if shield:
                return await shielded(run_off_loop(attr, *args, **kwargs))
            return await run_off_loop(attr, *args, **kwargs)
        _call.__name__ = name
        return _call

    def __repr__(self) -> str:
        return f"off_loop({self._target!r})"


def off_loop(obj: Any, *, shield: bool = False) -> Any:
    """A proxy of a sync store: ``await off_loop(store).method(...)`` runs in the store pool.

    ``shield=True`` is for a terminal write: each call is :func:`shielded` — a cancel
    lets the write finish, then the ``CancelledError`` goes on.
    """
    return _OffLoop(obj, shield)


async def shielded(aw: Any) -> Any:
    """Await a terminal write so a cancel cannot cut it: the write finishes, then
    the ``CancelledError`` goes on (the code after the await does not run).
    """
    fut = asyncio.ensure_future(aw)
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        if fut.cancelled():
            raise
        while not fut.done():
            try:
                await asyncio.wait({fut})
            except asyncio.CancelledError:
                continue
        if not fut.cancelled() and fut.exception() is not None:
            logger.warning("a shielded write failed after a cancel", exc_info=fut.exception())
        raise
