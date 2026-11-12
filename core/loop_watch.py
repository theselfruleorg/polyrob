"""See a stalled event loop (102 C-13): every session shares one loop, and the step
timeout and the stall monitor run ON it, so nothing else can report a sync stall.

A coroutine beats and keeps the worst overshoot of the window; a daemon thread names
the blocking frames WHILE the loop is stuck, because a line logged after the stall
cannot. A C call that holds the GIL (a regex) stops the thread too: the beat still
measures and logs that stall, without the frames.
"""
import asyncio
import logging
import sys
import threading
import time
import traceback
from typing import Optional

from core.async_bridge import spawn_retained

logger = logging.getLogger(__name__)

BEAT_S = 0.25
STALL_S = 2.0
POLL_S = 0.5
#: A beat that overshoots by more than this is counted as one stall (the
#: status line: "N stall(s) over 1 s"). Logged only over ``STALL_S``.
COUNT_S = 1.0


class _Watch:
    def __init__(self, loop: asyncio.AbstractEventLoop, thread_id: int) -> None:
        self.loop = loop
        self.thread_id = thread_id
        self.beat = time.monotonic()
        self.lag_max_s: Optional[float] = None
        self.stalls = 0
        #: The innermost frame of OUR code in the last stuck stack, so status
        #: names the blocking call and nobody has to grep the service log.
        self.site: Optional[str] = None
        self.stop = threading.Event()


_current: Optional[_Watch] = None
#: Its own registry: the beat outlives every other retained task, by design.
_TASKS: set = set()


async def _beat(watch: _Watch) -> None:
    try:
        while True:
            before = time.monotonic()
            await asyncio.sleep(BEAT_S)
            now = time.monotonic()
            lag = max(0.0, now - before - BEAT_S)
            watch.lag_max_s = max(watch.lag_max_s or 0.0, lag)
            watch.beat = now
            if lag > COUNT_S:
                watch.stalls += 1
            if lag > STALL_S:
                logger.warning("event loop stalled for %.1f s", lag)
    finally:
        watch.stop.set()


def _monitor(watch: _Watch) -> None:
    reported = None
    while not watch.stop.wait(POLL_S):
        if watch.loop.is_closed():
            return
        beat = watch.beat
        if time.monotonic() - beat <= STALL_S or beat == reported or not watch.loop.is_running():
            continue
        reported = beat
        frame = sys._current_frames().get(watch.thread_id)
        stack = "".join(traceback.format_stack(frame, limit=15)) if frame else "(no frame)\n"
        watch.site = _own_site(frame) or watch.site
        logger.warning("event loop stuck for over %.0f s at:\n%s", STALL_S, stack.rstrip())


def _own_site(frame) -> Optional[str]:
    """``pkg/module.py:line in func`` of the innermost frame that is not the
    standard library or an installed package; None when there is none."""
    try:
        for fs in reversed(traceback.extract_stack(frame, limit=30)):
            name = fs.filename.replace("\\", "/")
            if "/site-packages/" in name or "/lib/python" in name or name.startswith("<"):
                continue
            return f"{'/'.join(name.split('/')[-3:])}:{fs.lineno} in {fs.name}"
    except Exception:
        return None
    return None


def start() -> None:
    """Watch the running loop; the second call on one loop does nothing. Fail-open."""
    global _current
    try:
        loop = asyncio.get_running_loop()
        if _current is not None and _current.loop is loop and not _current.stop.is_set():
            return
        if _current is not None:
            _current.stop.set()
        watch = _Watch(loop, threading.get_ident())
        spawn_retained(_beat(watch), _TASKS)
        threading.Thread(target=_monitor, args=(watch,), name="polyrob-loop-watch",
                         daemon=True).start()
        _current = watch
    except Exception as e:  # liveness reporting never blocks startup
        logger.warning("loop watch not started: %s", e)


def take_window() -> Optional[dict]:
    """``{"lag_ms_max", "stalls", "site"}`` since the last read, then reset.
    None = NOT measured (no watch in this process, or no beat yet)."""
    watch = _current
    if watch is None or watch.stop.is_set() or watch.lag_max_s is None:
        return None
    out = {"lag_ms_max": int(watch.lag_max_s * 1000), "stalls": int(watch.stalls),
           "site": watch.site}
    watch.lag_max_s, watch.stalls, watch.site = None, 0, None
    return out


def take_lag_ms_max() -> Optional[int]:
    """The worst overshoot since the last read, then reset. None = NOT measured
    (no watch in this process, or no beat yet), which is not the same as no lag."""
    watch = _current
    if watch is None or watch.stop.is_set() or watch.lag_max_s is None:
        return None
    lag, watch.lag_max_s = watch.lag_max_s, None
    return int(lag * 1000)
