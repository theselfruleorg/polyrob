"""One long-lived daemon thread per stdio pipe.

``asyncio.wait_for(asyncio.to_thread(stream.readline), t)`` cancels the future, not the
thread. Each idle poll left a thread blocked in ``readline``: the process-wide default
executor filled, every ``to_thread`` in the process queued behind it, and an abandoned
thread read the next reply and dropped it. A pipe read has no timeout, so one thread owns
the pipe for its whole life and a caller only waits on what that thread delivered.
"""
import asyncio
import logging
import threading
from collections import deque
from typing import Optional

#: Lines read and not yet consumed. A full buffer stops the read, so the child blocks on
#: its own pipe as it would with no reader at all, and a silent consumer cannot grow RAM.
DEFAULT_MAX_PENDING = 256


class LineReader:
    """Deliver ``stream.readline()`` results to ``get`` on ``loop``."""

    def __init__(self, stream, loop: asyncio.AbstractEventLoop, *, name: str,
                 max_pending: int = DEFAULT_MAX_PENDING) -> None:
        self.stream = stream
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self._slots = threading.Semaphore(max_pending)
        self._stop = threading.Event()
        self._eof = False
        self._thread = threading.Thread(target=self._run, name=f"mcp-stdout-{name}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if not self._slots.acquire(timeout=1.0):
                    continue
                line = self.stream.readline()
                if not self._deliver(line) or not line:
                    return
        except Exception as e:  # a closed stream: the caller gets the reason
            self._deliver(e)

    def _deliver(self, item) -> bool:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
            return True
        except RuntimeError:  # the loop closed: nobody is left to read
            self._stop.set()
            return False

    async def get(self, timeout: Optional[float]) -> str:
        """The next line, or ``""`` at EOF (and on every call after it). Raises
        ``asyncio.TimeoutError`` when nothing arrived; the read itself goes on."""
        if self._eof:
            return ""
        item = await asyncio.wait_for(self._queue.get(), timeout)
        self._slots.release()
        if isinstance(item, Exception):
            raise item
        if not item:
            self._eof = True
        return item

    def pending(self) -> int:
        return self._queue.qsize()

    def stop(self) -> None:
        """No further read starts. A read already blocked ends at the pipe's EOF."""
        self._stop.set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout)


class StderrDrain:
    """Keep the child from blocking on a full stderr pipe, and hold the last lines for a
    failure report. Nothing else reads this pipe: a ``read()`` on the event loop blocks
    every session while a grandchild holds the write end open."""

    def __init__(self, stream, *, logger: Optional[logging.Logger] = None, max_tail: int = 50) -> None:
        self._stream = stream
        self._logger = logger
        self._tail: deque = deque(maxlen=max_tail)
        self._thread = threading.Thread(target=self._run, name="mcp-stderr", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for line in iter(self._stream.readline, ""):
                line = line.rstrip("\n")
                self._tail.append(line)
                if self._logger is not None:
                    self._logger.debug("MCP stderr: %s", line)
        except Exception as e:
            if self._logger is not None:
                self._logger.debug("Stderr reader ended: %s", e)

    def tail(self) -> str:
        return "\n".join(self._tail)

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout)
