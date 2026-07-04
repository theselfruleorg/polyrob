"""persistent_loop.py — turn runner + scheduler for the bottom-anchored REPL (D5).

Under ``POLYROB_PERSISTENT_INPUT`` the REPL is a long-lived prompt_toolkit
``Application`` (``cli/ui/app.py::build_app``) and each turn runs as a background
task while the bottom input/status region stays painted + live. This module holds
the two pieces that are pure control flow (and so are unit-testable without a
terminal):

- ``run_turn`` — run ONE turn: optional slash dispatch, then ``convo.respond``,
  with cancel + error rendered (never raised) and the live-usage poll. Mirrors the
  ephemeral loop's per-line body so behaviour matches the legacy path.
- ``TurnController`` — schedules at most one in-flight turn (the interactive idle
  gate assumes one turn at a time) and cancels it on Ctrl-C.

The Application wiring (run_async + run_in_terminal print routing) lives in
``cli/commands/chat.py``. This is the default REPL path on a TTY
(``POLYROB_PERSISTENT_INPUT`` default ON; set 0/off for the legacy ephemeral prompt).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from cli.ui.commands import ReplExit
from cli.ui.lifecycle import TurnLifecycle, TurnOutcome


def lifecycle_of(renderer: Any) -> Optional[TurnLifecycle]:
    """The ``TurnLifecycle`` owned by the renderer's ``SessionState`` (or None).

    Defensive — a renderer-less or lifecycle-less path (legacy/tests) returns
    None so the turn seam degrades to a no-op rather than raising.
    """
    state = getattr(renderer, "state", None)
    return getattr(state, "lifecycle", None)


async def run_turn(
    convo: Any,
    line: str,
    renderer: Any,
    *,
    on_turn_complete: Optional[Callable[[], None]] = None,
    slash_dispatch: Optional[Callable[[str], Awaitable[bool]]] = None,
    request_exit: Optional[Callable[[], None]] = None,
) -> None:
    """Run one REPL turn. Never raises — errors/cancel are rendered + swallowed.

    The turn boundary drives the ``TurnLifecycle`` (begin on submit → end on
    deliver/error/cancel), guarded by ``try/finally`` + a turn token so ``end_turn``
    fires EXACTLY once regardless of exit path and a stale cancelled task can't
    settle a later turn.
    """
    if slash_dispatch is not None and line.startswith("/"):
        try:
            handled = await slash_dispatch(line)
        except ReplExit:
            if request_exit is not None:
                request_exit()
            return
        if handled:
            return
        # not a recognized slash → fall through and treat as a turn

    lifecycle = lifecycle_of(renderer)
    token = lifecycle.begin_turn() if lifecycle is not None else 0
    # Project the lifecycle word onto the status bar the instant the turn starts so
    # the pinned bar reads "working" + the spinner animates immediately (the first
    # feed event can be seconds away behind the LLM call).
    sync_status(renderer)
    outcome = TurnOutcome.OK
    try:
        if renderer is not None:
            renderer.on_turn_start(line)

        try:
            from core.interactive_gate import interactive_turn

            with interactive_turn():
                answer = await convo.respond(line)
        except asyncio.CancelledError:
            outcome = TurnOutcome.CANCELLED
            _render_interrupt(renderer)  # Ctrl-C → back to idle, not a stuck spinner
            return
        except Exception as exc:
            outcome = TurnOutcome.ERROR
            _render_error(renderer, exc)  # error → terminal status, spinner stops
            return

        if on_turn_complete is not None:
            try:
                on_turn_complete()
            except Exception:
                pass

        if renderer is not None:
            renderer.on_turn_end(answer or "")
    finally:
        if lifecycle is not None:
            lifecycle.end_turn(token, outcome)
        # Settle the status bar from the (now-updated) lifecycle: ready on success,
        # sticky error/stopped on failure/cancel. A perpetual "working" reads as stuck.
        sync_status(renderer)


def sync_status(renderer: Any) -> None:
    """Project the lifecycle's derived word onto ``state.status`` (the bar's text).

    ``status`` is a memoized projection of ``TurnLifecycle.status_word()`` written
    ONLY here, at the turn seam — feed events never touch it, so a background turn
    can't flip an idle bar to "working". Best-effort: never raises into the loop.
    """
    try:
        state = getattr(renderer, "state", None)
        lifecycle = getattr(state, "lifecycle", None)
        if state is not None and lifecycle is not None:
            state.status = lifecycle.status_word()
    except Exception:
        pass


def _render_interrupt(renderer: Any) -> None:
    if renderer is None:
        return
    from cli.ui.events import ErrorEvent

    try:
        renderer.on_event(ErrorEvent(error_message="Turn interrupted.", error_type="interrupted"))
        renderer.on_turn_end("")
    except Exception:
        pass


def _render_error(renderer: Any, exc: Exception) -> None:
    if renderer is None:
        return
    from cli.ui.events import ErrorEvent

    try:
        renderer.on_event(ErrorEvent(error_message=str(exc), error_type=type(exc).__name__))
        renderer.on_turn_end("")
    except Exception:
        pass


class TurnController:
    """Run at most one turn at a time; cancel the in-flight one on interrupt.

    Args:
        run_coro_factory: ``(line) -> coroutine`` producing the turn coroutine.
        schedule:         ``(coro) -> task`` scheduler (e.g. the Application's
                          ``create_background_task`` or ``loop.create_task``).
    """

    def __init__(
        self,
        *,
        run_coro_factory: Callable[[str], Any],
        schedule: Callable[[Any], Any],
    ) -> None:
        self._factory = run_coro_factory
        self._schedule = schedule
        self._task: Optional[Any] = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def submit(self, line: str) -> None:
        """Schedule a turn for *line* (no-op if blank or a turn is already running)."""
        if not line.strip():
            return
        if self.busy:
            return  # one turn at a time (the interactive idle gate's invariant)
        self._task = self._schedule(self._factory(line))

    def interrupt(self) -> None:
        """Cancel the in-flight turn (Ctrl-C); no-op if idle."""
        if self.busy:
            self._task.cancel()
