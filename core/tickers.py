"""Shared interval-ticker loop (consolidates cron/goal/curator) + the loop heartbeat.

098 CO-07: the old ``TickerSupervisor`` had no caller (the autonomy runtime starts
its loops through ``AutonomyHandles``) and was deleted.

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


def emit_loop_heartbeats(entries, *, source: str = "supervisor") -> None:
    """Record one ``autonomy_tick`` per ``(name, task)`` — the ONE liveness
    emitter (2026-08-28: ``polyrob.app.autonomy_runtime.AutonomyHandles``, which the
    API lifespan / REPL / Telegram surface actually use, never emitted; prod had
    zero heartbeats in 16k telemetry rows). Also prunes the log to its retention
    window. Fail-open."""
    try:
        from core.event_log import get_event_log, event_log_enabled
        if not event_log_enabled():
            return
        log = get_event_log()
        from core.loop_watch import take_lag_ms_max
        lag = take_lag_ms_max()
        extra = {} if lag is None else {"loop_lag_ms_max": lag}
        for name, task in entries:
            alive = not task.done()
            log.record("autonomy_tick", source=source, loop=name,
                       alive=alive, reason=None if alive else "task_exited", **extra)
        # Keep the event log bounded (retention discipline; fail-open).
        try:
            import time as _t
            days = max(1, int_env("TELEMETRY_EVENT_LOG_RETENTION_DAYS", 30))
            log.prune(older_than_ts=_t.time() - days * 86400)
        except Exception:
            logger.debug("autonomy_tick: event-log prune skipped", exc_info=True)
    except Exception:
        pass
