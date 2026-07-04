"""blocks.py — pure Rich-renderable builders for the POLYROB CLI (Phase 2).

Every function here is PURE: it takes a typed ``RenderEvent`` (or primitive
fields) and returns a Rich renderable (``Text``, ``Panel``, ``Group``, …).
No I/O, no Console, no global state — so each builder is snapshot-testable via
``Console(record=True)`` / ``Console(file=StringIO)``.

Layout reference: proposal §7.2.  The action / tool shapes are the REAL
captured shapes (§0 amendment 3): step actions are
``{action_type, name, service, params{...}}``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

from cli.ui import dialog
from cli.ui.identity import agent_display_name
from cli.ui.events import (
    ErrorEvent,
    SessionDone,
    Step,
    ToolExec,
)
from cli.ui.secrets import scrub_secrets, scrub_then_cap
from cli.ui.theme import ICONS, fmt_tokens, style

# Reasoning longer than this many lines is collapsed with a /verbose hint.
_REASONING_COLLAPSE_LINES = 8
# Args longer than this are summarised rather than printed in full.
_ARG_SUMMARY_THRESHOLD = 60
# Tool result preview cap (chars) for the default tool transcript.
_RESULT_PREVIEW_CAP = 160


# ---------------------------------------------------------------------------
# Step header
# ---------------------------------------------------------------------------


def step_header(
    step: int,
    *,
    token_count: Optional[int] = None,
    duration_seconds: Optional[float] = None,
) -> Text:
    """``▸ Step 3                       1.2k tok · 0.8s`` — header line.

    The meta (tokens / duration) is right-of-header dim text; either may be
    omitted.
    """
    header = Text()
    header.append(f"{ICONS.step} Step {step}", style=style("step_header"))

    meta_parts: List[str] = []
    if token_count is not None:
        meta_parts.append(f"{fmt_tokens(token_count)} tok")
    if duration_seconds is not None and duration_seconds > 0:
        meta_parts.append(f"{duration_seconds:.1f}s")
    if meta_parts:
        header.append("   ")
        header.append(
            f" {ICONS.bullet} ".join(meta_parts), style=style("meta")
        )
    return header


# ---------------------------------------------------------------------------
# Reasoning panel
# ---------------------------------------------------------------------------


def reasoning_panel(reasoning: str, *, verbose: bool = False) -> Optional[Panel]:
    """Rounded dim panel of the agent's reasoning (soft-wrapped, never hard-cut).

    Collapses to the first ``_REASONING_COLLAPSE_LINES`` lines with a
    ``(+N lines — /verbose)`` hint only when it exceeds that.  Returns ``None``
    for empty reasoning (caller skips it).

    Args:
        verbose: when True (toggled via ``/verbose``) the collapse is disabled
            and the full reasoning is shown — the hint refers to this toggle.
    """
    text = (reasoning or "").strip()
    if not text:
        return None

    lines = text.splitlines()
    extra = 0
    if not verbose and len(lines) > _REASONING_COLLAPSE_LINES:
        extra = len(lines) - _REASONING_COLLAPSE_LINES
        lines = lines[:_REASONING_COLLAPSE_LINES]

    body = Text("\n".join(lines), style=style("reasoning_text"))
    if extra:
        body.append(
            f"\n(+{extra} lines — /verbose)", style=style("meta")
        )

    return Panel(
        body,
        title="reasoning",
        title_align="left",
        border_style=style("reasoning_border"),
        padding=(0, 1),
    )


# ---------------------------------------------------------------------------
# Tool call / result line
# ---------------------------------------------------------------------------


def tool_call_line(action: Dict[str, Any]) -> Text:
    """``→ name(smart-args)`` for one step action (real shape, §0 amendment 3).

    Action shape: ``{action_type, name, service, params{...}}``.  We label with
    the most specific name available (``action_type`` is the verb, e.g.
    ``send_message``); args are the ``params`` dict, smart-formatted.
    """
    name = (
        action.get("action_type")
        or action.get("name")
        or action.get("service")
        or "?"
    )
    params = action.get("params")
    args_str = _format_args(params)

    line = Text()
    line.append(f"  {ICONS.arrow} ", style=style("tool_call"))
    line.append(name, style=style("tool_name"))
    line.append("(")
    _append_args(line, params, args_str)
    line.append(")")
    return line


def tool_call_line_from_exec(event: ToolExec) -> Text:
    """The ``→ name(args)`` call line built from a ``tool_execution`` event.

    The event carries both the ``parameters`` and the result, so the call line can be
    emitted paired correctly (right before the ``✓`` result) instead of from the
    terminal Step event (which fires after execution → printed the pair inverted).
    """
    return tool_call_line({
        "action_type": event.action_name,
        "name": event.action_name,
        "service": event.tool_name,
        "params": event.parameters,
    })


def tool_result_suffix(
    *,
    success: bool,
    duration_seconds: float = 0.0,
    error: Optional[str] = None,
    detail: Optional[str] = None,
) -> Text:
    """``✓ 0.2s`` / ``✗ err`` result marker (printed after a tool resolves)."""
    out = Text()
    if success:
        out.append(f"  {ICONS.ok}", style=style("tool_ok"))
        tail = detail or (f"{duration_seconds:.1f}s" if duration_seconds else "")
        if tail:
            out.append(f" {tail}", style=style("meta"))
    else:
        out.append(f"  {ICONS.fail}", style=style("tool_fail"))
        msg = error or "error"
        out.append(f" {_summarize(msg)}", style=style("tool_fail"))
    return out


def tool_result_line(event: ToolExec) -> Text:
    """``  ✓ read_file · 0.2s · <scrubbed preview>`` — the result of one tool exec.

    Pairs (visually, indented) under the ``→ name(args)`` call line. On success
    shows the action name, duration, and a secret-scrubbed + length-capped result
    preview; on failure shows the error. Secrets are scrubbed BEFORE the length
    cap so a token can't survive half-cut.
    """
    line = Text()
    name = event.action_name or event.tool_name or "tool"
    if event.success:
        line.append(f"  {ICONS.ok} ", style=style("tool_ok"))
        line.append(name, style=style("tool_name"))
        meta_parts: List[str] = []
        if event.duration_seconds:
            meta_parts.append(f"{event.duration_seconds:.1f}s")
        preview = scrub_then_cap(event.result_preview, limit=_RESULT_PREVIEW_CAP)
        if preview:
            if event.result_truncated and not preview.endswith("…"):
                preview += "…"
            meta_parts.append(preview)
        if meta_parts:
            line.append(f" {ICONS.bullet} ", style=style("meta"))
            line.append(f" {ICONS.bullet} ".join(meta_parts), style=style("meta"))
    else:
        line.append(f"  {ICONS.fail} ", style=style("tool_fail"))
        line.append(name, style=style("tool_name"))
        msg = scrub_then_cap(event.error or "error", limit=_RESULT_PREVIEW_CAP)
        line.append(f" {ICONS.bullet} ", style=style("meta"))
        line.append(msg, style=style("tool_fail"))
    return line


# ---------------------------------------------------------------------------
# Full step block (header + reasoning + memory + tool lines)
# ---------------------------------------------------------------------------


def step_block(
    event: Step, *, token_count: Optional[int] = None, verbose: bool = False
) -> RenderableType:
    """Compose a step block from a ``Step`` event (scaffolding only).

    The agent-message bubble (``send_message`` text) is rendered SEPARATELY by
    the renderer — this builder only paints the demoted telemetry scaffolding:
    header, reasoning, memory, and the NON-message tool lines.

    Dialog-first demotion (proposal §dialog):
    - The post-hoc ``Executed: …`` reasoning echo is skipped (shown dim only
      under ``/verbose``).
    - The ``send_message(...)→…`` memory echo is hidden (shown only under
      ``/verbose``).
    - The ``→ send_message(...)`` tool line is suppressed (the bubble replaces
      it) unless ``/verbose``.

    token_count: optional override; falls back to the step's
    ``data.context.metrics.token_count`` when present.
    verbose: when True nothing is demoted — the full scaffolding (echo
        reasoning dimmed, memory, the suppressed send_message line) is shown.
        Toggled at runtime via the ``/verbose`` slash command.
    """
    if token_count is None:
        token_count = _step_token_count(event)

    parts: List[RenderableType] = [
        step_header(event.step, token_count=token_count)
    ]

    # Reasoning: skip the post-hoc ``Executed: …`` echo unless verbose.
    if verbose or not dialog.is_echo_reasoning(event.reasoning):
        panel = reasoning_panel(event.reasoning, verbose=verbose)
        if panel is not None:
            parts.append(panel)

    # Memory: hide the action-echo memory line unless verbose.
    if event.memory and (verbose or not dialog.is_echo_memory(event.memory)):
        mem = Text()
        mem.append("  memory: ", style=style("meta"))
        mem.append(_summarize(event.memory.strip(), limit=120), style=style("memory"))
        parts.append(mem)

    for action in event.actions:
        # The send_message bubble replaces its tool line; show it only in verbose.
        if not verbose and dialog.is_send_message_action(action):
            continue
        parts.append(tool_call_line(action))

    return Group(*parts)


def subagent_line(agent_name: str, step: int, summary: str = "") -> Text:
    """One dim collapsed line for a sub-agent step (proposal §14 grouping)."""
    line = Text()
    line.append(f"  {ICONS.subagent} {agent_name}", style=style("subagent"))
    line.append(f" step {step}", style=style("subagent"))
    if summary:
        line.append(f": {_summarize(summary, limit=60)}", style=style("subagent"))
    return line


# ---------------------------------------------------------------------------
# Completion summary panel
# ---------------------------------------------------------------------------


def completion_panel(
    event: SessionDone,
    *,
    tokens_total: Optional[int] = None,
    cost_estimate: Optional[float] = None,
    elapsed_seconds: Optional[float] = None,
    show_final_result: bool = True,
) -> Panel:
    """Session-end summary panel: status · steps · tokens · cost · duration · result.

    ``show_final_result`` gates whether the final-result text is included —
    set ``False`` in the REPL (the answer is rendered by ``on_turn_end``), set
    ``True`` in one-shot (``polyrob run``) where this panel IS the summary.  This is
    the double-render guard.
    """
    status_word = "completed" if event.success else "failed"
    border = style("summary_border") if event.success else style("summary_fail_border")

    meta_parts: List[str] = [status_word, f"{event.total_steps} steps"]
    if tokens_total:
        meta_parts.append(f"{fmt_tokens(tokens_total)} tok")
    if cost_estimate:
        meta_parts.append(f"${cost_estimate:.4f}")
    dur = elapsed_seconds if elapsed_seconds is not None else event.duration_seconds
    if dur:
        meta_parts.append(f"{dur:.1f}s")

    body = Text(f" {ICONS.bullet} ".join(meta_parts), style=style("meta"))
    if show_final_result and event.final_result:
        body.append("\n\n")
        body.append(event.final_result.strip(), style=style("answer"))

    return Panel(
        body,
        title="done",
        title_align="left",
        border_style=border,
        padding=(0, 1),
    )


# ---------------------------------------------------------------------------
# Error panel
# ---------------------------------------------------------------------------


def error_panel(event: ErrorEvent) -> Panel:
    """Red panel with the FULL error message (no truncation)."""
    msg = event.error_message or "(no message)"
    body = Text(msg, style=style("error_text"))
    title = f"{ICONS.error} error"
    if event.error_type:
        title = f"{ICONS.error} {event.error_type}"
    return Panel(
        body,
        title=title,
        title_align="left",
        border_style=style("error_border"),
        padding=(0, 1),
    )


# ---------------------------------------------------------------------------
# Agent message (the dialog hero)
# ---------------------------------------------------------------------------


def agent_message(text: str) -> RenderableType:
    """The HERO: an agent message rendered as a chat turn.

    Composition (dialog layer): a ``● rob`` speaker line, then the message body
    as full Rich ``Markdown`` indented under it — soft-wrapped, NEVER truncated,
    code blocks highlighted.  Used for both ``send_message`` text and final
    ``done`` answers, on both surfaces, so every agent utterance has one visual
    identity.  No panel border: a conversation should breathe, not stack boxes.
    """
    speaker = Text()
    speaker.append("● ", style=style("speaker_dot"))
    speaker.append(agent_display_name(), style=style("speaker_name"))
    body = Padding(Markdown((text or "").strip()), (0, 0, 0, 2))
    return Group(Text(""), speaker, body, Text(""))


def user_message(text: str) -> Optional[RenderableType]:
    """Echo the user's submitted turn instantly into the transcript (bug C).

    A thin ``❯ <text>`` line — caret in the prompt accent, text bold — printed the
    moment the user hits Enter, so their message lands in scrollback BEFORE the
    agent replies (Claude-Code parity). Multi-line input keeps its newlines.
    Returns ``None`` for a blank turn (the caller renders nothing).
    """
    flat = (text or "").strip()
    if not flat:
        return None
    line = Text()
    line.append(f"{ICONS.caret} ", style=style("user_caret"))
    line.append(flat, style=style("user_text"))
    return Group(Text(""), line)


def working_notice() -> Text:
    """One honest dim ``⋯ rob · working…`` line printed at turn start (REPL).

    Under the REPL the in-flight Rich ``Live`` spinner is suppressed (it corrupts
    the cursor under prompt_toolkit's ``patch_stdout``), and the bottom toolbar is
    frozen during a turn (``prompt_async`` has already returned). So a long,
    silent tool-running stretch would otherwise be dead-silent. This static,
    newline-terminated line (safe through the StdoutProxy) gives the user one
    "the agent is working" signal; real send_message bubbles + the answer print
    above the prompt as they arrive.
    """
    line = Text()
    line.append("⋯ ", style=style("meta"))
    line.append(agent_display_name(), style=style("speaker_name"))
    line.append(f" {ICONS.bullet} working…", style=style("meta"))
    return line


def no_final_message_notice(planning: Optional[str] = None) -> RenderableType:
    """Explicit notice that the agent ended its run without delivering a message.

    WS-3.1: a run that terminates on a brain-state/planning turn never called
    ``send_message`` or ``done`` with real prose — so the user would otherwise
    see only a dim planning line (or, before the token backstops, a raw
    ``{"current_state": …}`` dump) and NO answer.  Make the absence explicit and
    honest instead of silently presenting telemetry as the reply.  When a
    distilled goal is available it's shown as context for what the agent was
    last doing.
    """
    speaker = Text()
    speaker.append("● ", style=style("speaker_dot"))
    speaker.append(agent_display_name(), style=style("speaker_name"))
    notice = Text()
    notice.append("⚠ ", style=style("error_text"))
    notice.append("finished without a final message", style=style("meta"))
    parts: List[RenderableType] = [Text(""), speaker, Padding(notice, (0, 0, 0, 2))]
    if planning and planning.strip():
        ctx = Text()
        ctx.append("last goal: ", style=style("meta"))
        ctx.append(_summarize(planning.strip(), limit=120), style=style("memory"))
        parts.append(Padding(ctx, (0, 0, 0, 2)))
    parts.append(Text(""))
    return Group(*parts)


# ---------------------------------------------------------------------------
# Turn activity summary (one dim line in scrollback per non-trivial turn)
# ---------------------------------------------------------------------------


def turn_summary_line(
    *,
    steps: int = 0,
    tools: int = 0,
    tokens: int = 0,
    cost: float = 0.0,
    elapsed_seconds: float = 0.0,
    failed: bool = False,
) -> Text:
    """``● 3 steps · 2 tools · 14.2k tok · $0.0040 · 28s`` — the activity layer's
    one-line residue of a completed turn.  Zero-valued segments are omitted.
    """
    parts = dialog.summary_segments(
        steps=steps, tools=tools, tokens=tokens, cost=cost,
        elapsed_seconds=elapsed_seconds, failed=failed,
    )
    line = Text()
    line.append("● ", style=style("tool_fail") if failed else style("meta"))
    line.append(f" {ICONS.bullet} ".join(parts) or "done", style=style("meta"))
    return line


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize(text: str, limit: int = _ARG_SUMMARY_THRESHOLD) -> str:
    """Collapse newlines and cap *text* at *limit* chars with an ellipsis."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def _step_token_count(event: Step) -> Optional[int]:
    """Pull token_count out of a step's data.context.metrics, if present."""
    try:
        data = event.raw.get("data", {}) or {}
        metrics = (data.get("context", {}) or {}).get("metrics", {}) or {}
        tc = metrics.get("token_count")
        return int(tc) if tc is not None else None
    except Exception:
        return None


def _format_args(params: Any) -> str:
    """Render a params dict into a smart-arg string (used for sizing)."""
    if not isinstance(params, dict) or not params:
        return ""
    pieces: List[str] = []
    for key, value in params.items():
        pieces.append(f"{key}={_format_value(value)}")
    return ", ".join(pieces)


def _format_value(value: Any) -> str:
    """Smart-format a single arg value: quote strings, summarise long blobs.

    String values are secret-scrubbed first so a token passed as a tool arg
    (e.g. ``set_env(value="sk-…")``) never reaches the terminal scrollback.
    """
    if isinstance(value, str):
        value = scrub_secrets(value)
        if len(value) > _ARG_SUMMARY_THRESHOLD:
            return f'"{_summarize(value)}"'
        return f'"{value}"'
    # Non-str (dict/list/number): scrub the repr too — a credential nested inside a
    # structured arg (e.g. headers={"Authorization": "Bearer sk-…"}) would otherwise
    # reach the terminal scrollback unredacted.
    return scrub_secrets(str(value))


def _append_args(line: Text, params: Any, args_str: str) -> None:
    """Append smart-highlighted args to *line*.

    Paths / quoted strings get the arg-string style; everything else plain.
    Falls back to the precomputed ``args_str`` when *params* isn't a dict.
    """
    if not isinstance(params, dict) or not params:
        if args_str:
            line.append(scrub_secrets(args_str))
        return
    first = True
    for key, value in params.items():
        if not first:
            line.append(", ")
        first = False
        line.append(f"{key}=", style=style("meta"))
        if isinstance(value, str):
            scrubbed = scrub_secrets(value)
            rendered = _format_value(value)
            is_path = ("/" in scrubbed or "." in scrubbed) and " " not in scrubbed
            arg_style = style("tool_arg_path") if is_path else style("tool_arg_str")
            line.append(rendered, style=arg_style)
        else:
            # Nested dict/list value: scrub the repr so a credential inside a
            # structured arg isn't rendered verbatim into the transcript.
            line.append(scrub_secrets(str(value)))
