"""ResourceLifecycle: one state machine for an object that owns a connection,
process, container or handle (098 §3.5).

::

    NEW --start()--> STARTING --ok--> READY --probe fails--> DEGRADED --backoff--> STARTING
                        | fail           | stop()               | stop()
                        v                v                      v
                    DEGRADED          CLOSING ------------> CLOSED (restartable unless removed)

- One ``asyncio.Lock`` per resource; ``start``/``stop``/``restart`` are idempotent.
- ``ensure_ready()`` is the single entry for callers: READY returns the handle,
  DEGRADED with an elapsed backoff re-creates it, CLOSED raises
  :class:`ResourceClosed`.
- Every move into DEGRADED or CLOSING runs ``teardown`` first, so no path can
  replace a live handle without closing it.
- Backoff ``base * 2**n``, capped; the attempt counter resets on READY. There is
  no "max attempts per process life".
- :meth:`ResourceLifecycle.view` maps the state to a legacy status string for
  API callers (``ServerStatus``, ``ToolStatus``).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Awaitable, Callable, Generic, Mapping, Optional, TypeVar, Union

from core.state.machine import StateMachine

logger = logging.getLogger(__name__)

T = TypeVar("T")

NEW, STARTING, READY, DEGRADED, CLOSING, CLOSED, REMOVED = (
    "new", "starting", "ready", "degraded", "closing", "closed", "removed")

LIFECYCLE = StateMachine(
    "resource.lifecycle",
    states={NEW, STARTING, READY, DEGRADED, CLOSING, CLOSED, REMOVED},
    transitions={
        NEW: {STARTING, CLOSING},
        STARTING: {READY, DEGRADED, CLOSING},
        READY: {DEGRADED, CLOSING},
        DEGRADED: {STARTING, CLOSING},
        CLOSING: {CLOSED, REMOVED},
        CLOSED: {STARTING, CLOSING},
    },
    terminal={REMOVED},
    initial={NEW},
)


class ResourceClosed(Exception):
    """``ensure_ready()`` on a CLOSED/REMOVED resource."""


class ResourceUnavailable(Exception):
    """``ensure_ready()`` on a DEGRADED resource whose backoff has not elapsed,
    or whose re-create just failed. ``retry_in`` is seconds until the next try."""

    def __init__(self, name: str, retry_in: float, error: Optional[BaseException] = None):
        msg = f"{name} unavailable; retry in {retry_in:.1f}s"
        if error is not None:
            msg += f" ({type(error).__name__}: {error})"
        super().__init__(msg)
        self.retry_in = retry_in
        self.error = error


Fn = Callable[..., Union[Any, Awaitable[Any]]]


async def _call(fn: Fn, *args: Any) -> Any:
    out = fn(*args)
    if inspect.isawaitable(out):
        out = await out
    return out


class ResourceLifecycle(Generic[T]):
    """Owns one handle. ``create()`` returns the handle (raise = failed start);
    ``teardown(handle)`` closes it; ``probe(handle)`` returns truthy when healthy.
    Each hook may be sync or async."""

    def __init__(self, name: str, *, create: Fn, teardown: Optional[Fn] = None,
                 probe: Optional[Fn] = None, backoff_base: float = 1.0,
                 backoff_cap: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self._create = create
        self._teardown = teardown
        self._probe = probe
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self._clock = clock
        self._state = NEW
        self._handle: Optional[T] = None
        self._lock: Optional[asyncio.Lock] = None
        self.attempts = 0            # consecutive failures since the last READY
        self.last_error: Optional[BaseException] = None
        self.next_attempt_at = 0.0
        self.ready_since: Optional[float] = None

    # -- read-only view ------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def handle(self) -> Optional[T]:
        return self._handle

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def is_ready(self) -> bool:
        return self._state == READY

    def is_closed(self) -> bool:
        return self._state in (CLOSED, REMOVED)

    def due(self) -> bool:
        """DEGRADED and the backoff has elapsed (or NEW): a re-create may run."""
        if self._state == NEW:
            return True
        return self._state == DEGRADED and self._clock() >= self.next_attempt_at

    def retry_in(self) -> float:
        return max(0.0, self.next_attempt_at - self._clock())

    def backoff(self) -> float:
        n = max(0, self.attempts - 1)
        return min(self.backoff_cap, self.backoff_base * (2 ** min(n, 32)))

    def view(self, mapping: Mapping[str, Any], default: Any = None) -> Any:
        """Legacy status for API callers, e.g. ``{READY: ServerStatus.CONNECTED, ...}``."""
        return mapping.get(self._state, default)

    # -- transitions (caller holds no lock; each takes the resource lock) ----

    def _move(self, to: str) -> None:
        LIFECYCLE.check(self._state, to, ctx={"resource": self.name})
        self._state = to

    async def _teardown_handle(self) -> None:
        handle, self._handle = self._handle, None
        self.ready_since = None
        if handle is None or self._teardown is None:
            return
        try:
            await _call(self._teardown, handle)
        except Exception as e:  # a failed close never blocks the move
            logger.warning("lifecycle %s: teardown failed: %s", self.name, e)

    async def _start_locked(self) -> T:
        if self._state == READY and self._handle is not None:
            return self._handle
        if self._state == REMOVED:
            raise ResourceClosed(f"{self.name} was removed")
        if self._handle is not None:          # never replace a live handle unclosed
            await self._teardown_handle()
        self._move(STARTING)
        try:
            handle = await _call(self._create)
        except asyncio.CancelledError:
            await self._enter_degraded(None)
            raise
        except Exception as e:
            await self._enter_degraded(e)
            raise
        self._handle = handle
        self._move(READY)
        self.attempts = 0
        self.last_error = None
        self.next_attempt_at = 0.0
        self.ready_since = self._clock()
        return handle

    async def _enter_degraded(self, error: Optional[BaseException]) -> None:
        await self._teardown_handle()
        self._move(DEGRADED)
        self.attempts += 1
        self.last_error = error
        self.next_attempt_at = self._clock() + self.backoff()

    async def start(self) -> T:
        """Create the handle (idempotent: READY returns it). A failed create
        leaves DEGRADED with a backoff and re-raises."""
        async with self.lock:
            return await self._start_locked()

    async def stop(self, *, remove: bool = False) -> None:
        """Tear down and move to CLOSED (REMOVED when ``remove``). Idempotent."""
        async with self.lock:
            if self._state == REMOVED:
                return
            if self._state == CLOSED and not remove:
                return
            self._move(CLOSING)
            try:
                await self._teardown_handle()
            finally:
                # 098 review: a cancel during the teardown await (a shutdown
                # gather) must not leave CLOSING — no edge leaves it but these.
                self._move(REMOVED if remove else CLOSED)

    async def restart(self) -> T:
        """Tear down whatever is live and create again (also from CLOSED)."""
        async with self.lock:
            if self._state == READY:
                await self._enter_degraded(None)
                self.attempts = 0
            return await self._start_locked()

    async def fail(self, error: Optional[BaseException] = None) -> None:
        """A caller saw the handle break: teardown, DEGRADED with backoff."""
        async with self.lock:
            if self._state in (READY, STARTING):
                await self._enter_degraded(error)

    async def probe(self) -> bool:
        """READY: run the probe; a falsy result or a raise moves to DEGRADED.
        DEGRADED/NEW with an elapsed backoff: try a re-create. Returns READY-ness."""
        async with self.lock:
            if self._state == READY:
                if self._probe is None:
                    return True
                try:
                    ok = bool(await _call(self._probe, self._handle))
                    err: Optional[BaseException] = None
                except Exception as e:
                    ok, err = False, e
                if not ok:
                    await self._enter_degraded(err)
                return ok
            if self.due():
                try:
                    await self._start_locked()
                except Exception:
                    return False
                return True
            return False

    async def ensure_ready(self) -> T:
        """The single entry for callers."""
        async with self.lock:
            if self._state == READY and self._handle is not None:
                return self._handle
            if self._state in (CLOSED, REMOVED, CLOSING):
                raise ResourceClosed(f"{self.name} is {self._state}")
            if self._state == DEGRADED and not self.due():
                raise ResourceUnavailable(self.name, self.retry_in(), self.last_error)
            try:
                return await self._start_locked()
            except ResourceClosed:
                raise
            except Exception as e:
                raise ResourceUnavailable(self.name, self.retry_in(), e) from e

    def adopt(self, handle: T) -> None:
        """Record a handle created outside :meth:`start` (sync construction
        paths) as READY. Only from NEW/DEGRADED/CLOSED; the caller owns ordering."""
        if self._state == READY:
            raise RuntimeError(f"{self.name}: adopt over a live handle")
        self._move(STARTING)
        self._handle = handle
        self._move(READY)
        self.attempts = 0
        self.last_error = None
        self.ready_since = self._clock()
