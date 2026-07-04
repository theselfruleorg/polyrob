"""Renderer ABC for the POLYROB CLI.

Defines the interface that all concrete renderers (PlainRenderer, RichRenderer)
must implement, plus the shared per-turn bookkeeping every renderer needs for
the three-layer composition (dialog / activity / trace):

- ``on_event`` is a template method: it appends the event to the per-turn ring
  buffer and dispatches via the concrete leaf methods (``_handle_step``,
  ``_handle_error_event``, ``_handle_session_done``, ``_handle_trace_event``).
  Errors from leaf methods are swallowed so rendering bugs never break the loop.
- ``on_turn_start`` resets the ring buffer, the bubble-dedup state, and
  snapshots token/cost counters so per-turn metrics (``turn_steps``/
  ``turn_tool_calls``/``turn_tokens``/``turn_cost``/``turn_elapsed``) can be
  derived at turn end for the activity summary line.  Concrete overrides MUST
  call ``super().on_turn_start(...)``.
- ``render_trace`` replays the buffered events through the concrete
  ``_render_trace_event`` — the ``/steps`` retro-trace.  It renders the TRACE
  layer only (step scaffolding, tool lines, lifecycle markers), never re-prints
  dialog content.

**Bubble-dedup state** (R2 backstop):
  ``_message_bubble_rendered`` / ``_last_bubble_text`` track whether an agent
  message bubble was already shown this turn and what text it contained.  These
  live here so both PlainRenderer and RichRenderer share one implementation;
  subclasses call ``_mark_bubble_rendered(text)`` when they print a bubble and
  ``_is_bubble_repeat(text)`` to guard against a byte-identical second render.

Concrete implementations:
    PlainRenderer  — ``cli/ui/plain_renderer.py``
    RichRenderer   — ``cli/ui/rich_renderer.py``
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, List

from cli.ui import dialog
from cli.ui.events import ErrorEvent, RenderEvent, SessionDone, Step, ToolExec
from cli.ui.state import SessionState

#: Ring-buffer cap: enough for the longest realistic turn, bounded so a runaway
#: session can't grow memory without limit.
_TURN_BUFFER_MAX = 500


class Renderer(ABC):
    """Abstract base class for CLI renderers.

    Subclasses receive events and state and are responsible for all user-
    visible output.  They must never raise — ``on_event`` swallows errors from
    the leaf dispatch methods so a rendering bug can never break the agent loop.

    Constructor:
        ``__init__(self, state: SessionState)`` — renderers hold a reference
        to the shared ``SessionState`` so they can read it on demand (e.g.
        in ``render_status``).
    """

    def __init__(self, state: SessionState) -> None:
        self._state = state
        #: Verbosity toggle (flipped by the ``/verbose`` slash command).  When
        #: True the trace layer renders live (step blocks, tool lines, full
        #: reasoning); when False only dialog + activity are visible.
        self.verbose: bool = False
        #: Tool-transcript toggle (flipped by ``/quiet``).  ON by default: each
        #: tool call (name + scrubbed args) and its result (status + duration +
        #: truncated preview) render as finalized lines so the user sees what the
        #: agent is doing, not just a spinner.  This is a SEPARATE axis from
        #: ``verbose``: ``/quiet`` mutes the tool lines for a clean chat;
        #: ``/verbose`` is a SUPERSET (full raw trace).  ``verbose`` therefore
        #: never shows LESS than the default tool view.
        self.show_tools: bool = True
        # Per-turn bookkeeping (reset in on_turn_start).
        self._turn_events: List[RenderEvent] = []
        self._turn_started_at: float = time.monotonic()
        self._turn_tokens0: int = 0
        self._turn_cost0: float = 0.0
        # Bubble-dedup state (R2 backstop): track whether a send_message bubble
        # was already rendered this turn and what its text was.  Both
        # PlainRenderer and RichRenderer share this state via the base so the
        # guard is never duplicated.  Reset by on_turn_start.
        self._message_bubble_rendered: bool = False
        self._last_bubble_text: str = ""

    @property
    def state(self) -> SessionState:
        """The shared ``SessionState`` accumulator."""
        return self._state

    # ------------------------------------------------------------------
    # Bubble-dedup helpers (R2 backstop — shared by both renderers)
    # ------------------------------------------------------------------

    def _is_bubble_repeat(self, text: str) -> bool:
        """True when *text* is a byte-identical repeat of the bubble already shown.

        R2 backstop: a single turn may emit a send_message on more than one step
        (e.g. an agent that re-greets without calling done()).  Suppress a
        byte-identical repeat so the user never sees the same bubble twice.
        Distinct messages always render; the real fix is the conversational-exit
        in the agent loop (R1), which prevents the re-greet in the first place.
        """
        return self._message_bubble_rendered and text.strip() == self._last_bubble_text

    def _mark_bubble_rendered(self, text: str) -> None:
        """Record that a bubble with *text* was shown this turn."""
        self._message_bubble_rendered = True
        self._last_bubble_text = text.strip()

    # ------------------------------------------------------------------
    # Core event handling (template method + leaf dispatch)
    # ------------------------------------------------------------------

    def on_event(self, event: RenderEvent) -> None:
        """Buffer one normalised feed event and dispatch to the leaf handler.

        Never raises — leaf exceptions are swallowed so a rendering bug can
        never break the agent loop.

        Called for every event after ``state.update(event)`` has already been
        called, so the state reflects the latest values.
        """
        if len(self._turn_events) < _TURN_BUFFER_MAX:
            self._turn_events.append(event)
        try:
            self._render_event(event)
        except Exception:  # pragma: no cover - render must not crash the loop
            pass

    def _render_event(self, event: RenderEvent) -> None:
        """Dispatch one event to the appropriate leaf handler.

        Layer rules:
        - ``Step``         → ``_handle_step`` (dialog + optional trace)
        - ``ErrorEvent``   → ``_handle_error_event`` (always dialog layer)
        - ``SessionDone``  → ``_handle_session_done`` (dialog + optional trace)
        - everything else  → ``_handle_trace_event`` only when verbose

        Subclasses MUST NOT override this method; implement the leaf ``_handle_*``
        methods instead.
        """
        if isinstance(event, Step):
            self._handle_step(event)
            return

        if isinstance(event, ErrorEvent):
            self._handle_error_event(event)
            return

        if isinstance(event, SessionDone):
            self._handle_session_done(event)
            return

        # Tool transcript: a completed tool execution renders as a finalized
        # result line BY DEFAULT (show_tools), pulled out of the verbose-only
        # TRACE catch-all so the user sees every tool call without /verbose.
        # The concrete handler applies the show_tools/verbose, send_message/done,
        # and sub-agent gates. (render_trace() still replays ToolExec via the
        # _handle_trace_event path for /steps.)
        if isinstance(event, ToolExec):
            self._handle_tool_exec(event)
            return

        # Extension seam (D2): a registered event renders by its spec's layer +
        # render_line, via the renderer-neutral _emit_line — so a new core event
        # needs ZERO edits here or in either concrete renderer.
        from cli.ui.event_registry import Layer, RegisteredEvent, get_spec
        if isinstance(event, RegisteredEvent):
            spec = get_spec(event.type)
            if spec is None or spec.render_line is None:
                return
            if spec.layer is Layer.TRACE and not self.verbose:
                return
            if spec.layer is Layer.ACTIVITY:
                return  # ACTIVITY events touch state / the live region, not scrollback
            try:
                line = spec.render_line(event)
            except Exception:  # pragma: no cover - a bad spec must not crash the loop
                return
            if line:
                self._emit_line(line, dim=(spec.layer is Layer.TRACE))
            return

        # Everything else is trace — live only when verbose.
        if self.verbose:
            self._handle_trace_event(event)

    # ------------------------------------------------------------------
    # Leaf handlers — subclasses implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def _handle_step(self, event: Step) -> None:
        """Render a step event (dialog message + optional trace scaffolding)."""

    @abstractmethod
    def _handle_error_event(self, event: ErrorEvent) -> None:
        """Render an error event (always dialog layer)."""

    @abstractmethod
    def _handle_session_done(self, event: SessionDone) -> None:
        """Render a session-done event (dialog if failed + optional trace)."""

    def _handle_tool_exec(self, event: ToolExec) -> None:
        """Render a completed tool execution as a finalized result line.

        Default no-op; concrete renderers override to print the
        ``✓ name · dur · preview`` / ``✗ name · error`` line. The override is
        responsible for the gates: render only when ``show_tools or verbose``,
        skip dialog-channel actions (send_message/done), and suppress sub-agent
        tool results in the default view (``state.last_step_sub_agent``).
        """
        return

    def _should_show_tool(self, action_name: str) -> bool:
        """Shared gate for a tool line (call-start or result).

        True when the tool transcript should render this action:
        - ``show_tools or verbose`` is on (``/quiet`` mutes both off),
        - the action is real tool work (not send_message/done — those are the
          dialog bubble), and
        - it is NOT a sub-agent's tool in the default (non-verbose) view.
        """
        if not (self.show_tools or self.verbose):
            return False
        if dialog.is_dialog_action_name(action_name):
            return False
        if not self.verbose and getattr(self._state, "last_step_sub_agent", False):
            return False
        return True

    @abstractmethod
    def _handle_trace_event(self, event: RenderEvent) -> None:
        """Render any event in full trace form (step scaffolding, tool lines, etc.).

        Called live when ``verbose=True`` for non-Step/Error/Done events, and
        replayed by ``render_trace()`` for ALL buffered event types.
        Subclasses alias this from their ``_render_trace_event`` implementation.
        """

    @abstractmethod
    def _emit_line(self, text: str, *, dim: bool = False) -> None:
        """Emit one plain line of text in this renderer's surface (D2 seam).

        Renderer-neutral: a registered event's ``render_line`` returns a ``str``
        and the base routes it here, so a new event renders in both Rich and
        Plain with no per-renderer code. ``dim`` requests the muted/meta style.
        """

    @abstractmethod
    def on_stream_delta(self, delta: str) -> None:
        """Handle a token-streaming delta from the LLM.

        Args:
            delta: A text chunk from the LLM stream callback.
        """

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def on_turn_start(self, turn_text: str) -> None:
        """Called just before ``Conversation.respond()`` is awaited.

        Resets the per-turn ring buffer, the bubble-dedup state, and snapshots
        the token/cost counters.  Concrete overrides MUST call
        ``super().on_turn_start(turn_text)``.

        Args:
            turn_text: The user's message for this turn.
        """
        self._turn_events = []
        self._turn_started_at = time.monotonic()
        self._turn_tokens0 = self._state.tokens_total
        self._turn_cost0 = self._state.cost_estimate_total
        # Reset bubble-dedup state for the new turn.
        self._message_bubble_rendered = False
        self._last_bubble_text = ""

    @abstractmethod
    def on_turn_end(self, answer: str) -> None:
        """Called after ``Conversation.respond()`` returns.

        Args:
            answer: The agent's final answer for this turn.
        """

    # ------------------------------------------------------------------
    # Per-turn metrics (derived from the ring buffer + state snapshots)
    # ------------------------------------------------------------------

    def turn_steps(self) -> int:
        """Number of main-agent step events seen this turn."""
        return sum(1 for e in self._turn_events if isinstance(e, Step))

    def turn_tool_calls(self) -> int:
        """Tool calls this turn, excluding the message channel itself.

        Counted from each step's ``actions`` list (the authoritative per-step
        record; ``tool_execution`` feed events would double-count them).
        ``send_message``/``done`` are how the agent talks, not work it did —
        counting them would make every chat turn look like tool activity.
        """
        count = 0
        for e in self._turn_events:
            if isinstance(e, Step):
                for action in e.actions:
                    if not dialog.is_send_message_action(action) and (
                        action.get("action_type") != "done"
                    ):
                        count += 1
        return count

    def turn_tokens(self) -> int:
        """Tokens consumed this turn (state delta since on_turn_start)."""
        return max(0, self._state.tokens_total - self._turn_tokens0)

    def turn_cost(self) -> float:
        """Estimated cost of this turn (state delta since on_turn_start)."""
        return max(0.0, self._state.cost_estimate_total - self._turn_cost0)

    def turn_elapsed(self) -> float:
        """Seconds since on_turn_start."""
        return time.monotonic() - self._turn_started_at

    def turn_is_trivial(self) -> bool:
        """True when the turn deserves no activity summary (a plain chat reply)."""
        return self.turn_steps() <= 1 and self.turn_tool_calls() == 0

    def turn_failed(self) -> bool:
        """True when this turn saw an error or a failed session-done.

        Derived from the per-turn event buffer (not ``state.status``, which is now
        a lifecycle projection that has reset by summary time). Covers both the
        feed-failure path (``SessionDone`` not success) and the exception path
        (``_render_error`` injects an ``ErrorEvent`` before ``on_turn_end``).
        """
        for e in self._turn_events:
            if isinstance(e, ErrorEvent):
                return True
            if isinstance(e, SessionDone) and not e.success:
                return True
        return False

    # ------------------------------------------------------------------
    # Trace on demand (/steps)
    # ------------------------------------------------------------------

    def render_trace(self) -> int:
        """Re-render the last turn's buffered TRACE events (the ``/steps`` view).

        Returns the number of events replayed (0 = nothing buffered).
        """
        for event in self._turn_events:
            try:
                self._handle_trace_event(event)
            except Exception:  # pragma: no cover - replay must not crash
                pass
        return len(self._turn_events)

    # ------------------------------------------------------------------
    # Status + block printing
    # ------------------------------------------------------------------

    @abstractmethod
    def render_status(self) -> None:
        """Render / refresh the status line (or toolbar).

        PlainRenderer: prints a single summary line.
        RichRenderer: repaints the bottom toolbar.
        """

    @abstractmethod
    def print_block(self, text: str, **kwargs: Any) -> None:
        """Print a generic text block to the output stream.

        Intended for slash-command output, notices, etc.

        Args:
            text: The text to display.
            **kwargs: Renderer-specific hints (e.g. ``style``, ``title``).
        """
