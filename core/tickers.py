"""Shared interval-ticker loop + lifespan supervisor (consolidates cron/goal/curator).

The canonical loop is lifted verbatim from ``cron/runner.py::CronTicker.run_forever``
so all four ticker variants share identical scheduling semantics.
"""
import asyncio
import logging
import os

from core.env import int_env
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def _heartbeat_interval_sec() -> int:
    try:
        return max(30, int_env("AUTONOMY_HEARTBEAT_INTERVAL_SEC", 300))
    except Exception:
        return 300


class IntervalTicker:
    """Run an async tick coroutine on a fixed interval until a stop event fires.

    Optionally backs off the interval on consecutive idle ticks (``is_active``)
    so a low-traffic ticker doesn't keep waking the process on a fixed cadence
    forever -- the dominant reason a single-user local CLI session never lets
    the OS scheduler (and on a laptop, App Nap) go idle. Backoff is strictly
    opt-in: omit ``is_active`` (every ticker built before this was added) and
    the loop is byte-identical to the original fixed-interval implementation.
    """

    def __init__(
        self,
        tick_coro: Callable[[], Awaitable[Any]],
        interval_seconds: int,
        *,
        is_active: Optional[Callable[[Any], bool]] = None,
        max_interval_seconds: Optional[int] = None,
        backoff_factor: float = 2.0,
    ) -> None:
        self.tick_coro = tick_coro
        self.interval_seconds = interval_seconds
        self._is_active = is_active
        self._max_interval = max_interval_seconds or interval_seconds
        self._backoff_factor = backoff_factor
        self._current_interval = interval_seconds

    async def run_forever(self, stop_event: Optional[asyncio.Event] = None) -> None:
        while not (stop_event is not None and stop_event.is_set()):
            active = True
            try:
                result = await self.tick_coro()
                if self._is_active is not None:
                    active = bool(self._is_active(result))
            except Exception as e:  # a tick must never kill the loop
                logger.error("ticker tick failed: %s", e, exc_info=True)
                active = True  # never back off after an error -- retry promptly

            if self._is_active is not None:
                if active:
                    self._current_interval = self.interval_seconds
                else:
                    self._current_interval = min(
                        self._max_interval,
                        max(self.interval_seconds, self._current_interval * self._backoff_factor),
                    )

            wait_s = self._current_interval
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(wait_s),
                    timeout=wait_s,
                )
            except asyncio.TimeoutError:
                pass


class TickerSupervisor:
    """Manage a collection of named ticker tasks for API lifespan use.

    Accepts any object with a ``run_forever(stop_event)`` coroutine method —
    :class:`IntervalTicker` instances or existing ticker classes (CronTicker,
    GoalTicker, CuratorTicker) that delegate their own ``run_forever`` here.

    Usage::

        sup = TickerSupervisor()
        sup.register("cron", cron_ticker, enabled=cron_enabled())
        await sup.start_all()   # on startup
        ...
        await sup.stop_all()    # on shutdown (in finally)
    """

    def __init__(self) -> None:
        self._entries: list = []            # [(name, ticker, enabled)]
        self._tasks: dict = {}              # name -> (task, stop_event)
        self._hb_task: Optional[asyncio.Task] = None
        self._hb_stop: Optional[asyncio.Event] = None

    def register(self, name: str, ticker: Any, enabled: bool) -> None:
        """Register a ticker.  Only enabled tickers are started in :meth:`start_all`."""
        self._entries.append((name, ticker, enabled))

    def _emit_heartbeats(self) -> None:
        """Record one autonomy_tick per running ticker (liveness signal).

        The audit (2026-07-04) found NO automated 'is the loop alive' signal — ops
        relied on a manual human tick-log. This makes idle-but-alive observable, and
        flags a ticker whose task has died. Fail-open; lazy import keeps core clean.
        """
        try:
            from agents.task.telemetry.event_log import get_event_log, event_log_enabled
            if not event_log_enabled():
                return
            log = get_event_log()
            for name, (task, _stop) in self._tasks.items():
                alive = not task.done()
                log.record("autonomy_tick", source="supervisor", loop=name,
                           alive=alive, reason=None if alive else "task_exited")
            # Keep the event log bounded (retention discipline; fail-open).
            try:
                import time as _t
                days = max(1, int_env("TELEMETRY_EVENT_LOG_RETENTION_DAYS", 30))
                log.prune(older_than_ts=_t.time() - days * 86400)
            except Exception:
                pass
        except Exception:
            pass

    async def _heartbeat_loop(self) -> None:
        interval = _heartbeat_interval_sec()
        while not (self._hb_stop is not None and self._hb_stop.is_set()):
            try:
                await asyncio.wait_for(self._hb_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._hb_stop is not None and self._hb_stop.is_set():
                break
            self._emit_heartbeats()

    async def start_all(self) -> None:
        """Start all enabled tickers as background asyncio tasks."""
        for name, ticker, enabled in self._entries:
            if not enabled:
                continue
            stop = asyncio.Event()
            task = asyncio.create_task(ticker.run_forever(stop_event=stop))
            self._tasks[name] = (task, stop)
            logger.info("✅ %s ticker started", name)
        # Liveness heartbeat (only if something is actually running).
        if self._tasks:
            self._hb_stop = asyncio.Event()
            self._hb_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_all(self) -> None:
        """Signal stop and cancel all running tickers, awaiting their completion."""
        if self._hb_stop is not None:
            self._hb_stop.set()
        if self._hb_task is not None:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except (asyncio.CancelledError, Exception):
                pass
            self._hb_task = None
        for name, (task, stop) in self._tasks.items():
            stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            logger.info("%s ticker stopped", name)
        self._tasks.clear()
