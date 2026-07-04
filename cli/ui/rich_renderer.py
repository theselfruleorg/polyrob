"""rich_renderer.py — Rich inline-scrollback renderer for the POLYROB CLI.

``RichRenderer`` owns a Rich ``Console`` on the real stdout and prints typed
events into the terminal's native scrollback.  Under prompt_toolkit's
``patch_stdout()`` (wired in ``app.py``) these prints stay above the pinned
input + bottom toolbar.

Three-layer composition (the chat recomposition):

- **Dialog** (always): agent messages — ``send_message`` text and final
  ``done`` answers — render as ``blocks.agent_message`` (speaker mark + full
  Markdown, never truncated); errors render as red panels, including a failed
  session's ``error_message``.
- **Activity**: one dim ``blocks.turn_summary_line`` at turn end for
  non-trivial turns.  (The live in-flight indicator is ``ActivityLine``,
  owned by this renderer's turn lifecycle.)
- **Trace** (hidden by default): step blocks, sub-agent lines, completion
  panels.  ``verbose=True`` (the ``/verbose`` toggle) renders them live;
  ``render_trace()`` (the ``/steps`` command) replays the last turn's buffer.

Double-render guard: the answer text is printed exactly once — by the
``send_message`` bubble when one rendered this turn, by the streamed box
otherwise, by ``on_turn_end``'s answer as the fallback.
"""

from __future__ import annotations

from typing import Any, Optional

from rich.console import Console

from cli.ui import blocks, dialog
from cli.ui.activity import ActivityLine
from cli.ui.events import (
    AgentEnd,
    AgentRegistration,
    ErrorEvent,
    Info,
    IterationDone,
    LLMCall,
    RenderEvent,
    SessionDone,
    SessionStart,
    Step,
    ToolExec,
)
from cli.ui.renderer import Renderer
from cli.ui.state import SessionState
from cli.ui.streaming import ResponseBox
from cli.ui.theme import no_color, style


class RichRenderer(Renderer):
    """Rich renderer: dialog-first styled output into native scrollback.

    Args:
        state:   Shared ``SessionState`` accumulator.
        console: Optional Rich ``Console`` (inject ``Console(file=StringIO)``
                 for headless tests).  Defaults to a stdout console with
                 ``NO_COLOR`` honoured.
        one_shot: When True (``polyrob run``) the final answer arrives via
                 ``on_turn_end`` from the stashed ``final_result`` — rendering
                 is identical to the REPL; the flag only gates the verbose
                 completion panel's result text (it IS the summary there).
    """

    def __init__(
        self,
        state: SessionState,
        console: Optional[Console] = None,
        *,
        one_shot: bool = False,
        live_allowed: bool = True,
    ) -> None:
        super().__init__(state)
        # file=None (NOT sys.stdout): Rich then resolves ``sys.stdout`` DYNAMICALLY
        # on every write. This is load-bearing for the persistent input app, which
        # runs the whole REPL under prompt_toolkit's ``patch_stdout()`` — that swaps
        # ``sys.stdout`` for a proxy which routes prints through ``run_in_terminal``
        # so they land cleanly ABOVE the pinned input box. Capturing ``sys.stdout``
        # at construction (before patch_stdout) bypassed the proxy → output collided
        # with the app's box rendering (duplicated frame, text inside the border).
        self._console: Console = console if console is not None else Console(
            file=None,
            no_color=no_color(),
            highlight=False,
            soft_wrap=False,
        )
        self._one_shot = one_shot
        # P0 — Rich ``Live`` (the activity spinner + streaming box) corrupts the
        # display when it runs under prompt_toolkit's ``patch_stdout`` (the REPL):
        # Rich's cursor-up/erase-line escapes and prompt_toolkit's line-buffered
        # StdoutProxy fight for the cursor → stacked "thinking" lines + stray
        # ``\`` artifacts.  When ``live_allowed`` is False (set by the REPL) the
        # activity line stays dormant (the pinned ``bottom_toolbar`` is the
        # in-flight indicator) and the streaming box runs buffer-only, printing
        # the finalized answer once as a newline-terminated block.  ``rob run``
        # (no patch_stdout) keeps ``live_allowed=True`` and the live boxes.
        self._live_allowed = live_allowed
        # True when a persistent bottom status bar provides the live in-flight
        # indicator (the spinner repaints during the turn). When set, the static
        # ``working…`` turn-start line is suppressed — the box's spinner is the
        # signal, so the transcript stays clean (user msg → tools → answer).
        self.live_status_bar = False
        # The live streaming response box.  Receives deltas via
        # on_stream_delta and is finalized in on_turn_end.  _box_rendered tracks
        # the double-render guard: when the box rendered the answer this turn,
        # on_turn_end finalizes it instead of printing the answer again.
        self._box: Optional[ResponseBox] = None
        self._box_rendered = False
        # Verbose one-shot double-render guard: True once a completion panel has
        # already shown the final-result text this turn, so on_turn_end must not
        # reprint it as a separate answer block.
        self._completion_showed_answer = False
        # NOTE: _message_bubble_rendered / _last_bubble_text live in Renderer
        # base (the shared bubble-dedup state).  No local copies needed.
        # The one transient in-flight indicator (activity layer).  Created per
        # turn in on_turn_start, fed from Step events, stopped before the
        # streaming box opens (one Live per console) and at on_turn_end.
        self._activity: Optional[ActivityLine] = None

    # ------------------------------------------------------------------
    # Leaf handlers (called by Renderer._render_event dispatch)
    # ------------------------------------------------------------------

    def _handle_step(self, event: Step) -> None:
        """Feed activity, then render the step (dialog + optional trace)."""
        self._note_activity(event)
        self._render_step(event)

    def _handle_error_event(self, event: ErrorEvent) -> None:
        """Dialog layer: errors always surface as panels."""
        self._console.print(blocks.error_panel(event))

    def _handle_tool_exec(self, event: ToolExec) -> None:
        """Render a completed tool execution (finalized line, no Live).

        Verbose is the raw firehose: delegate to the trace renderer (every
        execution, incl. send_message/done, with the legacy detail + preview).
        Default view: the concise pretty line, gated by show_tools + the
        dialog-action / sub-agent filters in ``_should_show_tool``.
        """
        if self.verbose:
            self._handle_trace_event(event)
            return
        if not self._should_show_tool(event.action_name or event.tool_name):
            return
        # Emit the `→ name(args)` call line HERE (from the tool_execution event, which
        # carries both parameters AND result) immediately before the `✓` result — the
        # terminal Step event fires AFTER execution, so emitting the call line from it
        # printed the pair inverted (✓ before →). See blocks.tool_call_line_from_exec.
        self._console.print(blocks.tool_call_line_from_exec(event))
        self._console.print(blocks.tool_result_line(event))

    def _handle_session_done(self, event: SessionDone) -> None:
        # Dialog layer: a failed session's error explains itself or it's noise.
        if not event.success and event.error_message:
            self._console.print(
                blocks.error_panel(
                    ErrorEvent(
                        error_message=event.error_message,
                        error_type="session failed",
                    )
                )
            )
        if not self.verbose:
            return  # activity summary at on_turn_end covers the rest

        final = (event.final_result or "").strip()
        already_bubbled = (
            self._message_bubble_rendered and final == self._last_bubble_text
        )
        show_final = (
            self._one_shot
            and not already_bubbled
            and not dialog.is_plumbing_string(final)
        )
        self._console.print(
            blocks.completion_panel(
                event,
                tokens_total=self._state.tokens_total or None,
                cost_estimate=self._state.cost_estimate_total or None,
                elapsed_seconds=self._state.elapsed(),
                show_final_result=show_final,
            )
        )
        # When the verbose one-shot panel carried the final-result text it IS
        # the canonical answer — suppress on_turn_end's answer block.
        if show_final and final:
            self._completion_showed_answer = True

    def _render_step(self, event: Step) -> None:
        """Render a step: the message text is dialog; scaffolding is trace.

        Default view: ONLY the agent message (if the step carries one) — no
        header, no reasoning panel, no tool lines.  Verbose: the full step
        block first, then the message.  A sub-agent's step never reaches the
        dialog layer (its text isn't "rob" speaking): dim one-liner under
        verbose, nothing by default.
        """
        identity = self._event_agent_identity(event)
        if self._state.is_sub_agent(identity):
            if self.verbose:
                self._console.print(
                    blocks.subagent_line(
                        identity or "subagent", event.step, event.reasoning or ""
                    )
                )
            return

        if self.verbose:
            self._console.print(blocks.step_block(event, verbose=True))
        # NOTE: the `→ name(args)` tool-call line is emitted from the tool_execution
        # handler (paired correctly before `✓`), NOT here — the Step event is terminal
        # (fires after execution), so emitting it here inverted the pair.

        message_text = dialog.find_message_text(event.actions)
        if message_text is not None:
            # R2 backstop via base: skip a byte-identical repeat bubble.
            if self._is_bubble_repeat(message_text):
                return
            self._console.print(blocks.agent_message(message_text))
            self._mark_bubble_rendered(message_text)

    @staticmethod
    def _event_agent_identity(event: Step) -> str:
        """Return the best available agent identity string for a step event.

        Preference order (real formatter shape):
          1. ``data.agent_name``   — always present in live feed
          2. top-level ``agent_name`` — backwards-compat duplicate
          3. ``data.agent_id``     — never emitted by current formatter, kept
                                     for defensive forwards-compat
          4. top-level ``agent_id``

        Returns "" when none of the above are present.
        """
        data = event.raw.get("data", {}) or {}
        return str(
            data.get("agent_name")
            or event.raw.get("agent_name")
            or data.get("agent_id")
            or event.raw.get("agent_id")
            or ""
        )

    # ------------------------------------------------------------------
    # Trace layer (live under /verbose; replayed by render_trace / /steps)
    # ------------------------------------------------------------------

    def _handle_trace_event(self, event: RenderEvent) -> None:
        if isinstance(event, Step):
            identity = self._event_agent_identity(event)
            if self._state.is_sub_agent(identity):
                self._console.print(
                    blocks.subagent_line(
                        identity or "subagent", event.step, event.reasoning or ""
                    )
                )
            else:
                self._console.print(blocks.step_block(event, verbose=True))
            return

        if isinstance(event, ErrorEvent):
            self._console.print(blocks.error_panel(event))
            return

        if isinstance(event, SessionDone):
            self._console.print(
                blocks.completion_panel(
                    event,
                    tokens_total=self._state.tokens_total or None,
                    cost_estimate=self._state.cost_estimate_total or None,
                    elapsed_seconds=self._state.elapsed(),
                    show_final_result=False,
                )
            )
            return

        if isinstance(event, ToolExec):
            from cli.ui.secrets import scrub_then_cap
            status = "ok" if event.success else f"FAIL {event.error or ''}"
            preview = scrub_then_cap(event.result_preview, limit=160)
            tail = f" {preview}" if (event.success and preview) else ""
            self._console.print(
                f"  tool {event.tool_name}/{event.action_name} {status}{tail}",
                style=style("meta"),
            )
            return

        if isinstance(event, IterationDone):
            done_flag = " done" if event.is_done else ""
            self._console.print(
                f"  iter {event.iteration} {event.iteration_status}{done_flag}",
                style=style("meta"),
            )
            return

        if isinstance(event, LLMCall):
            self._console.print(
                f"  llm {event.model_name} {event.duration_seconds:.1f}s",
                style=style("meta"),
            )
            return

        if isinstance(event, (SessionStart, AgentRegistration, AgentEnd, Info)):
            label = getattr(event, "type", "info")
            self._console.print(f"  {label}", style=style("meta"))
            return

    # ------------------------------------------------------------------
    # Activity layer (the one transient in-flight line)
    # ------------------------------------------------------------------

    def _note_activity(self, event: Step) -> None:
        """Feed a completed step into the live activity line (if running)."""
        if self._activity is None:
            return
        tool_actions = sum(
            1
            for action in event.actions
            if not dialog.is_send_message_action(action)
            and action.get("action_type") != "done"
        )
        self._activity.note_step(event.step, tool_actions)

    def _stop_activity(self) -> None:
        if self._activity is not None:
            self._activity.stop()
            self._activity = None

    # ------------------------------------------------------------------
    # Streaming (the live response box)
    # ------------------------------------------------------------------

    def on_stream_delta(self, delta: str) -> None:
        """Route a streaming delta into the live response box.

        On the first delta of a turn this opens a Rich ``Live`` "rob" box; each
        subsequent delta appends + repaints.  1-or-N-chunk safe: a single
        full-answer chunk and many token chunks produce identical final content.
        When the console is non-TTY the box degrades to buffer-only and the text
        is printed once at ``on_turn_end``.

        The activity line MUST be stopped first: only one Rich ``Live`` can run
        per console, and the box is the turn's feedback from here on.
        """
        self._stop_activity()
        if self._box is None:
            # Under the REPL (live_allowed=False) the box runs buffer-only: no
            # Rich Live, so no cursor contention with prompt_toolkit. The text
            # still accumulates and is printed once at on_turn_end.
            box_console = self._console if self._live_allowed else None
            self._box = ResponseBox(console=box_console)
        self._box.append(delta)
        if self._box.received_chunk:
            self._box_rendered = True

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def on_turn_start(self, turn_text: str) -> None:
        """Reset per-turn streaming + dialog state (and the base ring buffer),
        then open the live activity line for this turn.

        The bubble-dedup state is reset by ``super().on_turn_start()``.
        The activity line is skipped under ``/verbose`` — the live trace IS the
        feedback there — and stays dormant on non-terminal consoles.
        """
        super().on_turn_start(turn_text)
        # Bug C: echo the user's line into the transcript instantly, so it lands
        # in scrollback the moment they submit — before the agent replies. Under
        # the persistent box (patch_stdout) this prints cleanly ABOVE the input.
        user_block = blocks.user_message(turn_text)
        if user_block is not None:
            self._console.print(user_block)
        self._box = None
        self._box_rendered = False
        self._completion_showed_answer = False
        # NOTE: _message_bubble_rendered / _last_bubble_text reset by super().
        self._stop_activity()
        if not self.verbose and self._live_allowed:
            self._activity = ActivityLine(self._console)
            self._activity.start()
        elif not self.verbose and not self._live_allowed and not self.live_status_bar:
            # Ephemeral REPL: the Live spinner is suppressed AND the bottom toolbar
            # is frozen during a turn (prompt_async has returned), so print one
            # honest static "working…" line — a silent tool stretch isn't
            # dead-silent. Skipped under the persistent app (live_status_bar): its
            # bottom box repaints a live spinner, so the static line is redundant.
            self._console.print(blocks.working_notice())

    def on_turn_end(self, answer: str) -> None:
        """Print the answer exactly once, then the activity summary line.

        Double-render guard: the send_message bubble (when one rendered), the
        streamed box content, the verbose one-shot completion panel, and the
        ``answer`` param are FOUR sources of the same text — exactly one wins,
        in that order.
        """
        self._stop_activity()
        self._finalize_answer(answer)
        self._print_turn_summary()

    def _finalize_answer(self, answer: str) -> None:
        # A send_message bubble was already rendered this turn.  In the REPL the
        # bubble IS the turn's voice — on_turn_end's ``answer`` is the plumbing
        # receipt or a redundant done-recap, so render nothing more.  In
        # one-shot the bubble may have been a mid-task progress note while the
        # real answer arrives as final_result: suppress only plumbing/echoes,
        # never NEW content (a preamble must not eat the answer).
        if self._message_bubble_rendered:
            norm = (answer or "").strip()
            if (
                not self._one_shot
                or not norm
                or dialog.is_plumbing_string(norm)
                or norm == self._last_bubble_text
                # OR-1: a bookkeeping done()-recap after a real reply is a confusing
                # 2nd bubble in one-shot — suppress it (REPL already does).
                or (dialog.SUPPRESS_DONE_RECAP and dialog.is_redundant_recap(norm, self._last_bubble_text))
            ):
                self._teardown_box()
                return

        # Suppress known plumbing receipts even when no bubble rendered (e.g. a
        # non-blocking send that didn't surface as a step in this view).
        if dialog.is_plumbing_string(answer):
            self._teardown_box()
            return

        # Verbose one-shot: the completion panel already carried the final
        # result — it's the canonical answer; just stop any live box.
        if self._completion_showed_answer:
            self._teardown_box()
            self._completion_showed_answer = False
            return

        if self._box is not None and self._box_rendered:
            streamed = self._box.finalize()
            # OR-7 text selection (SSOT in dialog.choose_answer_text).
            text = dialog.choose_answer_text(answer, streamed)
            if text and text.strip():
                # Finalize the box to a static, persistent block (the transient
                # Live has been stopped).  A tool-free planning turn streams raw
                # brain-state (some models as {"current_state": …} JSON) — that
                # is telemetry, not the agent's voice: demote it to one dim line.
                self._console.print(self._answer_or_planning(text))
            self._box = None
            self._box_rendered = False
            return

        # No stream chunks this turn → print the answer once.
        if answer and answer.strip():
            self._console.print(self._answer_or_planning(answer))

    @staticmethod
    def _answer_or_planning(text: str):
        """The terminal answer block for ``on_turn_end``.

        Normally an ``agent_message`` bubble.  But ``_finalize_answer`` only
        reaches here when NO ``send_message`` bubble rendered this turn — so if
        the terminal text is brain-state, the agent ended its whole run on a
        planning turn without ever delivering a real message (WS-3.1).  Surface
        that explicitly as a "finished without a final message" notice (with the
        distilled goal for context) rather than silently presenting telemetry —
        or, worse, a raw ``{"current_state": …}`` dump — as the reply.
        """
        if dialog.is_brain_state(text):
            line = dialog.brain_planning_line(text)
            return blocks.no_final_message_notice(line)
        return blocks.agent_message(text)

    def _teardown_box(self) -> None:
        if self._box is not None:
            self._box.finalize()
        self._box = None
        self._box_rendered = False

    def _print_turn_summary(self) -> None:
        """One dim activity line per non-trivial turn (the activity residue)."""
        if self.turn_is_trivial():
            return
        self._console.print(
            blocks.turn_summary_line(
                steps=self.turn_steps(),
                tools=self.turn_tool_calls(),
                tokens=self.turn_tokens(),
                cost=self.turn_cost(),
                elapsed_seconds=self.turn_elapsed(),
                failed=self.turn_failed(),
            )
        )

    # ------------------------------------------------------------------
    # Status + block printing
    # ------------------------------------------------------------------

    def render_status(self) -> None:
        """No-op: prompt_toolkit owns the bottom toolbar (it pulls from state)."""
        return

    def _emit_line(self, text: str, *, dim: bool = False) -> None:
        """D2 seam: print one registered-event line into scrollback."""
        self._console.print(text, style=style("meta") if dim else None)

    def print_block(self, text: str, **kwargs: Any) -> None:
        """Print a generic block (slash-command output, notices, etc.)."""
        title = kwargs.get("title")
        block_style = kwargs.get("style")
        if title:
            from rich.panel import Panel
            from rich.text import Text

            self._console.print(
                Panel(
                    Text(text, style=block_style or ""),
                    title=str(title),
                    title_align="left",
                )
            )
        else:
            self._console.print(text, style=block_style or None)

    # ------------------------------------------------------------------
    # Accessors used by app.py / the REPL
    # ------------------------------------------------------------------

    @property
    def console(self) -> Console:
        """The underlying Rich ``Console`` (real stdout in production)."""
        return self._console
