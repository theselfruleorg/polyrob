"""Shared interval-ticker loop (consolidates cron/goal/curator) + the loop heartbeat.

098 CO-07: the old ``TickerSupervisor`` had no caller (the autonomy runtime starts
its loops through ``AutonomyHandles``) and was deleted.

The canonical loop is lifted verbatim from ``cron/runner.py::CronTicker.run_forever``
so all four ticker variants share identical scheduling semantics.
"""
import asyncio
import logging
import os
import time
import weakref

from core.env import int_env
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def _heartbeat_interval_sec() -> int:
    try:
        return max(30, int_env("AUTONOMY_HEARTBEAT_INTERVAL_SEC", 300))
    except Exception:
        return 300


#: task -> the state of the ticker that runs in it. The heartbeat reads it, so
#: "alive" means "a tick completed", not "the task object exists" (116 W4.1).
_TICK_STATE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def tick_state(task) -> Optional[dict]:
    """The tick record of the ticker running in *task*, or None (not a ticker)."""
    try:
        return _TICK_STATE.get(task)
    except TypeError:
        return None


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
        state = {"interval_s": float(self.interval_seconds), "ok_at": None,
                 "running_since": None, "errors": 0, "error": ""}
        try:
            _TICK_STATE[asyncio.current_task()] = state
        except Exception:      # no running task (a direct call in a test): untracked
            pass
        while not (stop_event is not None and stop_event.is_set()):
            active = True
            state["running_since"] = time.time()
            try:
                result = await self.tick_coro()
                state["ok_at"], state["errors"], state["error"] = time.time(), 0, ""
                if self._is_active is not None:
                    active = bool(self._is_active(result))
            except Exception as e:  # a tick must never kill the loop
                logger.error("ticker tick failed: %s", e, exc_info=True)
                state["errors"] += 1
                state["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                active = True  # never back off after an error -- retry promptly
            finally:
                state["running_since"] = None

            if self._is_active is not None:
                if active:
                    self._current_interval = self.interval_seconds
                else:
                    self._current_interval = min(
                        self._max_interval,
                        max(self.interval_seconds, self._current_interval * self._backoff_factor),
                    )

            wait_s = self._current_interval
            state["interval_s"] = float(wait_s)
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(wait_s),
                    timeout=wait_s,
                )
            except asyncio.TimeoutError:
                pass


def heartbeat_attrs(name: str, task, *, common: Optional[dict] = None) -> dict:
    """One loop's ``autonomy_tick`` attributes. ``alive`` keeps its old meaning
    (the task has not exited) for a reader from an older process; the tick
    fields are what the liveness verdict reads (``core.status_loops``)."""
    alive = not task.done()
    attrs = {"loop": name, "alive": alive, "reason": None if alive else "task_exited"}
    st = tick_state(task)
    if st is not None:
        attrs.update({"tick_ok_at": st["ok_at"], "tick_running_since": st["running_since"],
                      "tick_errors": st["errors"], "tick_error": st["error"],
                      "interval_s": st["interval_s"]})
    attrs.update(common or {})
    return attrs


def emit_loop_heartbeats(entries, *, source: str = "supervisor") -> None:
    """Record one ``autonomy_tick`` per ``(name, task)`` — the ONE liveness
    emitter (2026-08-28: ``polyrob.app.autonomy_runtime.AutonomyHandles``, which the
    API lifespan / REPL / Telegram surface actually use, never emitted; prod had
    zero heartbeats in 16k telemetry rows). Every row names its process (pid +
    start time) and its emission (``hb``), and carries the loop-watch window:
    the worst lag, the stalls over 1 s and the last blocking site. Also prunes
    the log to its retention window. Fail-open (``core.event_log.emit``)."""
    try:
        from core.event_log import emit, event_log_enabled, get_event_log
        if not event_log_enabled():
            return
        common: dict = {"hb": int(time.time() * 1000), "pid": os.getpid()}
        try:
            from core.state.proc import process_identity
            common["pid_start"] = process_identity().start
        except Exception:
            logger.debug("autonomy_tick: no process start time", exc_info=True)
        from core.loop_watch import take_window
        win = take_window()
        if win is not None:
            common["loop_lag_ms_max"] = win["lag_ms_max"]
            common["loop_stalls"] = win["stalls"]
            if win.get("site"):
                common["loop_stall_site"] = win["site"]
        for name, task in entries:
            emit("autonomy_tick", source=source,
                 attrs=heartbeat_attrs(name, task, common=common))
        # Keep the event log bounded (retention discipline; fail-open).
        try:
            days = max(1, int_env("TELEMETRY_EVENT_LOG_RETENTION_DAYS", 30))
            get_event_log().prune(older_than_ts=time.time() - days * 86400)
        except Exception:
            logger.debug("autonomy_tick: event-log prune skipped", exc_info=True)
    except Exception:
        logger.warning("autonomy_tick: heartbeat not recorded", exc_info=True)
