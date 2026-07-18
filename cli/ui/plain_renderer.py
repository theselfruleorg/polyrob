"""PlainRenderer — deterministic, non-TTY, CI-safe CLI renderer.

Writes stable, line-oriented plain text to a file-like stream.  Same
three-layer composition as the Rich renderer, in degraded-chat form:

- **Dialog** (always): ``› {text}`` user echo, ``rob:`` indented message
  blocks (full text, never truncated), ``error: …`` lines (including a failed
  session's ``error_message``).
- **Activity**: one ``(N steps · M tools · X tok · $C · Ts)`` line per
  non-trivial turn (no live indicator — plain mode has no Live).
- **Trace** (hidden by default): the bracketed event lines.  ``verbose=True``
  renders them live; ``render_trace()`` (``/steps``) replays the last turn.

Design constraints:
- No ANSI escape codes.
- No external dependencies beyond the standard library.
- No hard truncation of reasoning or results.
- Deterministic output so golden-output tests are reliable.
- The ``stream`` constructor argument accepts any file-like object
  (``io.StringIO`` for tests, ``sys.stderr`` in production).

Trace prefix legend (verbose / ``/steps`` only):
  [session]   SessionStart
  [step N]    Step
  [llm]       LLMCall
  [tool]      ToolExec
  [iter]      IterationDone
  [done]      SessionDone
  [info]      Info / unknown
"""

from __future__ import annotations

import sys
from typing import Any, IO, Optional

from cli.ui import blocks, dialog
from cli.ui.identity import agent_display_name
from cli.ui.events import (
    AgentEnd,
    AgentRegistration,
    ErrorEvent,
    Info,
    IterationDone,
    LLMCall,
    LLMStarted,
    RenderEvent,
    SessionDone,
    SessionStart,
    Step,
    ToolExec,
    ToolStarted,
)
from cli.ui.renderer import Renderer
from cli.ui.state import SessionState


class PlainRenderer(Renderer):
    """Line-oriented plain-text renderer for non-TTY / ``--plain`` / CI use.

    Args:
        state:   Shared ``SessionState`` accumulator.
        stream:  Output stream.  Defaults to ``sys.stderr``.
    """

    def __init__(
        self,
        state: SessionState,
        stream: Optional[IO[str]] = None,
        *,
        one_shot: bool = False,
    ) -> None:
        super().__init__(state)
        # Default to stdout: every caller passes stream explicitly, but the old
        # stderr default was a footgun (a forgotten stream split output across fds
        # from the dialog on stdout). D1.
        self._stream: IO[str] = stream if stream is not None else sys.stdout
        self._stream_buffer: list[str] = []
        self._one_shot = one_shot
        # NOTE: _message_bubble_rendered / _last_bubble_text live in Renderer
        # base (the shared bubble-dedup state).  No local copies needed.

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, line: str) -> None:
        """Write *line* followed by a newline to the output stream."""
        print(line, file=self._stream)

    def _emit_line(self, text: str, *, dim: bool = False) -> None:
        """D2 seam: write one registered-event line (plain, no styling)."""
        self._write(text)

    def _write_bubble(self, text: str) -> None:
        """Write the agent's message as a ``rob:`` chat block.

        Multi-line text is preserved and indented so the message reads as a
        block, not buried in a tool-arg line.
        """
        body = (text or "").strip()
        self._write(f"{agent_display_name()}:")
        for line in body.splitlines() or [""]:
            self._write(f"  {line}")

    def _fmt_tokens(self) -> str:
        """Format token summary from state."""
        parts: list[str] = []
        if self._state.tokens_in:
            parts.append(f"in={self._state.tokens_in}")
        if self._state.tokens_out:
            parts.append(f"out={self._state.tokens_out}")
        if self._state.tokens_total:
            parts.append(f"total={self._state.tokens_total}")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Leaf handlers (called by Renderer._render_event dispatch)
    # ------------------------------------------------------------------

    def _handle_step(self, event: Step) -> None:
        """Render a step: message text is dialog; scaffolding is trace."""
        if self.verbose:
            self._render_step_trace(event)
        # The `→ name(args)` tool-call line is emitted from the tool_execution handler
        # (paired before `✓`), NOT here — the Step event is terminal (fires after
        # execution), so emitting it here inverted the pair.
        message_text = dialog.find_message_text(event.actions)
        if message_text is not None:
            # R2 backstop via base: skip a byte-identical repeat bubble.
            if self._is_bubble_repeat(message_text):
                return
            self._write_bubble(message_text)
            self._mark_bubble_rendered(message_text)

    def _handle_tool_exec(self, event: ToolExec) -> None:
        """Render a completed tool execution.

        Verbose is the raw firehose: delegate to the trace ``[tool]`` line (every
        execution, incl. send_message/done). Default view: the concise pretty
        line, gated by show_tools + the dialog-action / sub-agent filters.
        """
        if self.verbose:
            self._handle_trace_event(event)
            return
        # 019 span pairing: a paired completion prints ONLY the result line —
        # and prints it even if `_should_show_tool` now says no (a printed `→`
        # must never be left unclosed; see rich_renderer for the delegate_task
        # case). Unpaired completions keep the legacy two-line form.
        paired = self._consume_start_printed(event.call_id)
        if not paired and not self._should_show_tool(event.action_name or event.tool_name):
            return
        if not paired:
            self._write(blocks.tool_call_line_from_exec(event).plain.rstrip())
        self._write(blocks.tool_result_line(event).plain.rstrip())

    def _handle_tool_started(self, event: ToolStarted) -> None:
        """Print `→ name(args)` at DISPATCH time (019); same gates as the
        completion line."""
        if self.verbose:
            self._write(f"[tool] start {event.tool_name}/{event.action_name}")
            return
        if not self._should_show_tool(event.action_name or event.tool_name):
            return
        self._write(blocks.tool_call_line_from_started(event).plain.rstrip())
        self._note_start_printed(event.call_id)

    def _handle_llm_started(self, event: LLMStarted) -> None:
        """Trace-only: the plain surface has no live region to animate."""
        if self.verbose:
            model = event.model_name or event.provider or "llm"
            self._write(f"[llm] start {model} attempt={event.attempt}")

    def _handle_error_event(self, event: ErrorEvent) -> None:
        """Dialog layer: errors always surface (no bracket prefix)."""
        self._write(f"error: {event.error_type}: {event.error_message}")

    def _handle_session_done(self, event: SessionDone) -> None:
        """Dialog layer: a failed session must explain itself."""
        if not event.success and event.error_message:
            self._write(f"error: session failed: {event.error_message}")
        if self.verbose:
            self._handle_trace_event(event)

    # ------------------------------------------------------------------
    # Trace layer (live under /verbose; replayed by render_trace / /steps)
    # ------------------------------------------------------------------

    def _handle_trace_event(self, event: RenderEvent) -> None:  # noqa: PLR0912
        if isinstance(event, SessionStart):
            task_str = f" task={event.task!r}" if event.task else ""
            vision = " vision=yes" if event.use_vision else ""
            self._write(
                f"[session] start model={event.model_name}{task_str}{vision}"
            )

        elif isinstance(event, Step):
            self._render_step_trace(event)

        elif isinstance(event, LLMCall):
            parts = [f"model={event.model_name}"]
            if event.provider:
                parts.append(f"provider={event.provider}")
            if event.prompt_tokens is not None:
                parts.append(f"in={event.prompt_tokens}")
            if event.completion_tokens is not None:
                parts.append(f"out={event.completion_tokens}")
            if event.token_count is not None:
                parts.append(f"total={event.token_count}")
            if event.cost_estimate is not None:
                parts.append(f"cost=${event.cost_estimate:.6f}")
            parts.append(f"dur={event.duration_seconds:.2f}s")
            status = "ok" if event.success else "FAIL"
            parts.append(f"status={status}")
            self._write("[llm] " + " ".join(parts))

        elif isinstance(event, ToolExec):
            from cli.ui.secrets import scrub_then_cap
            status = "ok" if event.success else "fail"
            dur = f" dur={event.duration_seconds:.2f}s" if event.duration_seconds else ""
            err = f" error={event.error!r}" if event.error else ""
            preview = scrub_then_cap(event.result_preview, limit=160)
            prev = f" preview={preview!r}" if (event.success and preview) else ""
            self._write(
                f"[tool] {event.tool_name}/{event.action_name}"
                f" status={status}{dur}{err}{prev}"
            )

        elif isinstance(event, IterationDone):
            done_flag = " DONE" if event.is_done else ""
            self._write(
                f"[iter {event.iteration}] status={event.iteration_status}{done_flag}"
            )

        elif isinstance(event, ErrorEvent):
            self._write(
                f"[error] type={event.error_type} {event.error_message}"
            )

        elif isinstance(event, SessionDone):
            # Summary only — never include the result text here.
            # The answer is rendered exactly once via on_turn_end().
            status = "completed" if event.success else "FAILED"
            self._write(
                f"[done] {status} steps={event.total_steps}"
            )

        elif isinstance(event, (AgentRegistration, AgentEnd)):
            # State-only events — the RichRenderer treats these as silent too.
            # Emitting a line would add noise; silence them in plain mode.
            pass

        elif isinstance(event, Info):
            content_str = f" {event.content}" if event.content else ""
            self._write(f"[info] type={event.type}{content_str}")

        else:
            # Fallback for any future subtypes
            self._write(f"[event] {getattr(event, 'type', '?')}")

    def _render_step_trace(self, event: Step) -> None:
        """The full ``[step N]`` scaffolding for one step (trace layer)."""
        prefix = f"[step {event.step}]"
        if event.reasoning:
            for line in event.reasoning.splitlines():
                self._write(f"{prefix} reasoning: {line}")
        if event.memory:
            for line in event.memory.splitlines():
                self._write(f"{prefix} memory: {line}")
        for action in event.actions:
            name = action.get("name", action.get("action_type", "?"))
            params = action.get("params", "")
            param_str = f" {params}" if params else ""
            self._write(f"{prefix} → {name}{param_str}")

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def on_stream_delta(self, delta: str) -> None:
        """Buffer streaming deltas; printed at turn end."""
        self._stream_buffer.append(delta)

    def on_turn_start(self, turn_text: str) -> None:
        """Echo the user's turn (dialog layer) and reset per-turn state.

        The bubble-dedup state is reset by ``super().on_turn_start()``.
        """
        super().on_turn_start(turn_text)
        self._stream_buffer.clear()
        self._write(f"› {turn_text}")

    def on_turn_end(self, answer: str) -> None:
        """Print the answer exactly once, then the activity summary line.

        Dialog-first: in the REPL a rendered bubble is the turn's voice, so the
        ``answer`` (plumbing receipt / done-recap) is suppressed.  In one-shot
        the bubble may have been a progress note while the real answer is the
        final_result: only plumbing and bubble echoes are suppressed there.
        """
        buffered = "".join(self._stream_buffer)
        self._stream_buffer.clear()
        # OR-7 text selection (SSOT in dialog.choose_answer_text). _write_answer
        # still demotes brain-state as a backstop.
        text = dialog.choose_answer_text(answer, buffered)
        norm = (text or "").strip()
        suppressed_by_bubble = self._message_bubble_rendered and (
            not self._one_shot
            or norm == self._last_bubble_text
            or (dialog.SUPPRESS_DONE_RECAP and dialog.is_redundant_recap(norm, self._last_bubble_text))
        )
        if not suppressed_by_bubble:
            if not dialog.is_plumbing_string(norm) and norm:
                self._write_answer(text)
        self._print_turn_summary()

    def _write_answer(self, text: str) -> None:
        """Write the turn's final answer, demoting brain-state to a notice.

        OR-2: a tool-free turn whose content is brain-state (some providers — e.g.
        DeepSeek's non-native JSON-fallback path — emit it as a ```json fenced
        ``{"current_state": …}`` dump) never produced a real message. Surface the
        absence honestly instead of dumping telemetry as the reply, mirroring the
        rich renderer's ``_answer_or_planning``.
        """
        if dialog.is_brain_state(text):
            self._write(f"{agent_display_name()}:")
            self._write("  ⚠ finished without a final message")
            planning = dialog.brain_planning_line(text)
            if planning and planning.strip():
                self._write(f"  last goal: {planning.strip()}")
            return
        self._write_bubble(text)

    def _print_turn_summary(self) -> None:
        """One activity line per non-trivial turn (the plain-mode residue)."""
        if self.turn_is_trivial():
            return
        parts = dialog.summary_segments(
            steps=self.turn_steps(),
            tools=self.turn_tool_calls(),
            tokens=self.turn_tokens(),
            cost=self.turn_cost(),
            elapsed_seconds=self.turn_elapsed(),
            failed=self.turn_failed(),
        )
        self._write(f"({' · '.join(parts) or 'done'})")

    # ------------------------------------------------------------------
    # Status + block printing
    # ------------------------------------------------------------------

    def render_status(self) -> None:
        """Print a single-line status summary."""
        elapsed = self._state.elapsed()
        tok_str = self._fmt_tokens()
        cost = self._state.cost_estimate_total
        cost_str = f" cost=${cost:.6f}" if cost else ""
        ctx = self._state.ctx_percent
        ctx_str = f" ctx={ctx:.0f}%" if ctx else ""
        self._write(
            f"[status] {self._state.status}"
            f" step={self._state.step}"
            f" elapsed={elapsed:.1f}s"
            f"{(' ' + tok_str) if tok_str else ''}"
            f"{cost_str}{ctx_str}"
        )

    def print_block(self, text: str, **kwargs: Any) -> None:
        """Print a text block (for slash-command output, final answers, etc.)."""
        title: str = kwargs.get("title", "")
        if title:
            self._write(f"--- {title} ---")
        self._write(text)
