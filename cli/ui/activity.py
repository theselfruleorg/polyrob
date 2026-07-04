"""activity.py — the one transient in-flight indicator for the POLYROB CLI.

While ``Conversation.respond()`` runs (10–300s!), the user needs exactly ONE
calm, live signal that the agent is working — not a stream of step blocks:

    ⠋ rob · working · 2 tools · step 3 · 12s

``ActivityLine`` owns that line as a transient Rich ``Live``:

- ``start()`` opens the Live (TTY consoles only — on a non-terminal console it
  stays dormant, keeping CI/tests byte-deterministic).
- ``note_step``/``note_tool`` bump the counters; elapsed time and the spinner
  tick on the Live's own refresh thread via render-time composition (the
  renderable recomputes its text every repaint — no timers of our own).
- ``stop()`` clears it from the screen (transient).  Idempotent, thread-safe.

Exactly one Rich ``Live`` can run per console: the renderer MUST ``stop()``
this line before opening the streaming ``ResponseBox``.  Ordinary
``console.print`` calls while the line is live are fine — Rich moves them
above the live region — so mid-turn message bubbles need no special handling.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from cli.ui.theme import ICONS, style


class _ActivityRenderable:
    """Render-time bridge: recomputes the spinner text on every Live repaint."""

    def __init__(self, owner: "ActivityLine") -> None:
        self._owner = owner
        from rich.spinner import Spinner

        self._spinner = Spinner("dots", text="", style=style("status_running"))

    def __rich_console__(self, console: Any, options: Any) -> Any:
        self._spinner.update(text=self._owner.compose_text())
        yield self._spinner


class ActivityLine:
    """The single live "working…" line for one turn.

    Args:
        console: A Rich ``Console``.  The Live only starts when
                 ``console.is_terminal`` is true; otherwise the line stays
                 dormant (counters still accumulate, nothing is printed).
        clock:   Monotonic clock, injectable for tests.
    """

    def __init__(
        self,
        console: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._console = console
        self._clock = clock
        self._lock = threading.RLock()
        self._started_at = clock()
        self._steps = 0
        self._tools = 0
        self._live: Optional[Any] = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the transient Live (TTY only; best-effort, never raises)."""
        with self._lock:
            if self._live is not None or self._stopped:
                return
            if not getattr(self._console, "is_terminal", False):
                return
            try:
                from rich.live import Live

                live = Live(
                    _ActivityRenderable(self),
                    console=self._console,
                    refresh_per_second=4,  # was 10; a spinner/elapsed-time line
                    # doesn't need sub-250ms cadence, and each Hz here is a
                    # standing repaint-thread wakeup for the whole turn.
                    transient=True,
                )
                live.start()
                self._live = live
            except Exception:
                self._live = None

    def stop(self) -> None:
        """Clear the line from the screen.  Idempotent, never raises."""
        with self._lock:
            self._stopped = True
            if self._live is not None:
                try:
                    self._live.stop()
                except Exception:  # pragma: no cover - defensive
                    pass
                self._live = None

    @property
    def is_live(self) -> bool:
        with self._lock:
            return self._live is not None

    # ------------------------------------------------------------------
    # Counters (fed from Step events)
    # ------------------------------------------------------------------

    def note_step(self, step: int, tool_actions: int = 0) -> None:
        """Record one completed step and its non-message tool actions."""
        with self._lock:
            self._steps += 1
            self._tools += max(0, tool_actions)

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose_text(self) -> str:
        """``rob · working · 2 tools · step 3 · 12s`` (spinner glyph excluded)."""
        with self._lock:
            steps, tools = self._steps, self._tools
        sep = f" {ICONS.bullet} "
        from cli.ui.identity import agent_display_name
        parts = [agent_display_name(), "working" if (steps or tools) else "thinking"]
        if tools:
            parts.append(f"{tools} tool{'s' if tools != 1 else ''}")
        if steps:
            parts.append(f"step {steps}")
        elapsed = max(0.0, self._clock() - self._started_at)
        parts.append(f"{elapsed:.0f}s")
        return sep.join(parts)
