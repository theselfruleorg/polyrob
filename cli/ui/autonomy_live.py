"""The REPL's live handle on its autonomy loops (026 P5.2).

`rob` starts the autonomy runtime once at boot (``cli/commands/chat.py``). This
module holds that handle so REPL ``/autonomy on|off`` can start and stop the
loops in the SAME process, with no restart:

- ``on``  — writes ``AUTONOMY_ENABLED=true`` through the one write path with a
  live apply (``core.config_service.set_value(live=True)``), then (re)starts
  the runtime when this REPL runs one. A runtime started while autonomy was
  OFF did not start the self-directed loops, so it is stopped and started
  again under the new value.
- ``off`` — writes ``AUTONOMY_ENABLED=false`` live, then stops the runtime via
  the existing bounded ``AutonomyHandles.stop()``.

Only the REPL binds a controller; every other process has none and the verb
says "restart applies".
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class LiveAutonomy:
    """Owns the REPL's ``AutonomyHandles`` (or ``None`` when not running)."""

    def __init__(self, starter: Callable[[], Any], handles: Any = None) -> None:
        self._starter = starter
        self.handles = handles

    @property
    def running(self) -> bool:
        return self.handles is not None

    def start(self) -> Any:
        self.handles = self._starter()
        return self.handles

    async def stop(self) -> bool:
        """Stop the runtime; True when something was running."""
        h, self.handles = self.handles, None
        if h is None:
            return False
        try:
            await h.stop()
        except Exception:
            logger.warning("autonomy stop failed (non-fatal)", exc_info=True)
        return True

    async def restart(self) -> Any:
        await self.stop()
        return self.start()


_CURRENT: Optional[LiveAutonomy] = None


def bind(live: Optional[LiveAutonomy]) -> None:
    global _CURRENT
    _CURRENT = live


def current() -> Optional[LiveAutonomy]:
    return _CURRENT


async def autonomy_switch(on: bool, *, is_global: bool = True) -> str:
    """The body of REPL ``/autonomy on|off``. Returns the lines to show.

    The value persists in the home .env — the only env file the CLI loads
    (``./.polyrob/.env`` is never read); ``is_global`` is kept for callers."""
    from core.config_service import set_value
    res = set_value("AUTONOMY_ENABLED", "true" if on else "false",
                    scope="global", surface="local", live=True)
    if not res.ok:
        return f"error: {res.message}"
    lines = [res.message]
    live = current()
    if live is None:
        lines.append("this process runs no autonomy loops — the value applies "
                     "at the next start")
        return "\n".join(lines)
    try:
        if on:
            h = live.handles
            if h is not None and getattr(h, "autonomy_enabled", False):
                lines.append("loops: already running")
            else:
                await live.restart()
                lines.append("loops: started in this session (no restart)")
        else:
            stopped = await live.stop()
            lines.append("loops: stopped in this session" if stopped
                         else "loops: none were running")
            lines.append("for a pause that every process honours, use /pause")
    except Exception as e:
        lines.append(f"loops: could not {'start' if on else 'stop'} live "
                     f"({type(e).__name__}: {e}) — the value applies at the next start")
    return "\n".join(lines)


__all__ = ["LiveAutonomy", "autonomy_switch", "bind", "current"]
