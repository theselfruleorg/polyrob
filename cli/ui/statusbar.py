"""statusbar.py — bottom-toolbar line builder for the POLYROB CLI (Phase 2).

Pure builders (no I/O) that turn a ``SessionState`` into the bottom status
line shown by prompt_toolkit's ``bottom_toolbar``:

    {model} · {in}↑ {out}↓ · ctx {ctx}% · ${cost} · {elapsed} · {spinner}{status}

Two surfaces:
- ``status_text(state)`` — a plain ``str`` (used by the PlainRenderer and
  by tests; deterministic).
- ``status_formatted(state, spinner_frame)`` — prompt_toolkit
  ``FormattedText`` (used by the live toolbar).  Importing prompt_toolkit is
  deferred so the plain path has no hard dependency.

The spinner frame is supplied by the caller (the toolbar ticks it on a timer)
so this module stays pure and time-independent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple

from cli.ui.theme import fmt_tokens, ICONS
from cli.ui.state import SessionState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from prompt_toolkit.formatted_text import FormattedText


def _fmt_elapsed(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _autonomy_active(state: SessionState) -> bool:
    """True when a background (cron/goal/self-wake) turn is in flight.

    Orthogonal to the foreground work clock/status — a background turn lights this
    muted ``⟲ autonomy`` token WITHOUT touching the user's work clock or status word
    (a user who walked away never sees a runaway 'working' for autonomous work).
    """
    lifecycle = getattr(state, "lifecycle", None)
    return bool(lifecycle is not None and lifecycle.autonomy_busy())


#: The Claude-Code-style cooking glyph used when no animated spinner is supplied.
_COOKING_GLYPH = "✱ "


def _is_active(state: SessionState) -> bool:
    """True when a user turn is in flight (drives the cooking affordance)."""
    lifecycle = getattr(state, "lifecycle", None)
    return bool(lifecycle is not None and lifecycle.is_active())


def cooking_text(state: SessionState, spinner: str = "") -> str:
    """The live in-flight affordance: ``✱ cooking… 7s`` (Option A, issue 1).

    Rendered in the pinned region while the turn runs, replacing the bare status
    word + a separate elapsed segment. The ticking work clock
    (``lifecycle.active_elapsed``, frozen on idle so it shows work time not session
    age) is the liveness; an animated braille *spinner* (supplied by the persistent
    app at 5 Hz) is used as the pulse glyph when available, else a static ``✱``.
    """
    lifecycle = getattr(state, "lifecycle", None)
    elapsed = lifecycle.active_elapsed() if lifecycle is not None else 0.0
    glyph = spinner if spinner else _COOKING_GLYPH
    return f"{glyph}cooking… {_fmt_elapsed(elapsed)}"


def _segments(state: SessionState, spinner: str = "") -> List[str]:
    """The ordered list of status segments (joined by ' · ')."""
    segs: List[str] = []

    model = state.model or "—"
    segs.append(model)

    tok = f"{fmt_tokens(state.tokens_in)}{ICONS.up} {fmt_tokens(state.tokens_out)}{ICONS.down}"
    segs.append(tok)

    # Live-info (D3): in-flight tool + active sub-agent count. The tool is the
    # CURRENT action — shown only while a turn runs so it doesn't linger at the
    # idle prompt.
    last_tool = getattr(state, "last_tool", "")
    if last_tool and _is_active(state):
        segs.append(f"{ICONS.arrow}{last_tool}")
    n_sub = getattr(state, "subagents_active", 0)
    if n_sub:
        segs.append(f"{n_sub} sub-agent{'s' if n_sub != 1 else ''}")

    if _autonomy_active(state):
        segs.append("⟲ autonomy")

    if state.ctx_percent:
        segs.append(f"ctx {state.ctx_percent:.0f}%")

    segs.append(f"${state.cost_estimate_total:.4f}")

    # Tail: while a turn runs, ONE Claude-style ``✱ cooking… Xs`` affordance (work
    # clock folded in); when idle, the plain status word (ready/error/stopped) — no
    # clock, so the bar never counts session age at the prompt.
    if _is_active(state):
        segs.append(cooking_text(state, spinner))
    else:
        status = state.status or ""
        word = f"{spinner}{status}".strip()
        if word:
            segs.append(word)

    return segs


def autonomy_line(state: SessionState) -> str:
    """The second pinned line: model/provider + autonomy snapshot (D4).

    ``glm-5.2 · openrouter    autonomy: goals 1 · cron 2 · review on`` — built
    from ``state.autonomy_snapshot`` (a slow-polled, cached dict; never read the
    goal/cron SQLite stores on the hot repaint path). Returns "" when there is no
    snapshot, so the line is hidden until autonomy data is available. Pure.
    """
    snap = getattr(state, "autonomy_snapshot", None)
    if not snap:
        return ""
    auto: List[str] = []
    goals = int(snap.get("goals", 0) or 0)
    cron = int(snap.get("cron", 0) or 0)
    if goals:
        auto.append(f"goals {goals}")
    if cron:
        auto.append(f"cron {cron}")
    if snap.get("review"):
        auto.append("review on")
    left = state.model or "—"
    if state.provider:
        left = f"{left} {ICONS.bullet} {state.provider}"
    if not auto:
        return left
    return f"{left}    autonomy: " + f" {ICONS.bullet} ".join(auto)


def status_text(state: SessionState, spinner: str = "") -> str:
    """Build the plain-text status line (no ANSI)."""
    return f" {ICONS.bullet} ".join(s for s in _segments(state, spinner) if s)


# ---------------------------------------------------------------------------
# prompt_toolkit FormattedText surface
# ---------------------------------------------------------------------------


def _status_class(status: str) -> str:
    """Map a status word to a prompt_toolkit class name for styling.

    Lifecycle vocabulary is working / ready / error / stopped; legacy words
    (running/completed/done/failed) are still mapped for any injected/test state.
    """
    s = (status or "").lower()
    if s in ("error", "failed", "stopped"):
        return "class:status.error"
    if s in ("ready", "completed", "done"):
        return "class:status.ok"
    return "class:status.running"


def status_formatted(
    state: SessionState, spinner: str = "", *, include_model: bool = True
) -> "FormattedText":
    """Build a prompt_toolkit ``FormattedText`` status toolbar.

    Each segment carries a class so the toolbar can be themed; the final
    status word is colored by ``_status_class``. ``include_model=False`` omits
    the leading model segment (the framed persistent input shows the model on
    the box's top edge, so the status bar would otherwise repeat it).
    """
    from prompt_toolkit.formatted_text import FormattedText

    sep = f" {ICONS.bullet} "
    fragments: List[Tuple[str, str]] = [("", " ")]

    if include_model:
        model = state.model or "—"
        fragments.append(("class:toolbar.model", model))
        fragments.append(("", sep))

    fragments.append(
        (
            "class:toolbar.tokens",
            f"{fmt_tokens(state.tokens_in)}{ICONS.up} "
            f"{fmt_tokens(state.tokens_out)}{ICONS.down}",
        )
    )
    fragments.append(("", sep))

    # Live-info (D3): in-flight tool (only while a turn runs) + sub-agent count.
    last_tool = getattr(state, "last_tool", "")
    if last_tool and _is_active(state):
        fragments.append(("class:toolbar.tool", f"{ICONS.arrow}{last_tool}"))
        fragments.append(("", sep))
    n_sub = getattr(state, "subagents_active", 0)
    if n_sub:
        fragments.append(
            ("class:toolbar.subagents", f"{n_sub} sub-agent{'s' if n_sub != 1 else ''}")
        )
        fragments.append(("", sep))

    if _autonomy_active(state):
        fragments.append(("class:toolbar.autonomy", "⟲ autonomy"))
        fragments.append(("", sep))

    if state.ctx_percent:
        fragments.append(("class:toolbar.ctx", f"ctx {state.ctx_percent:.0f}%"))
        fragments.append(("", sep))

    fragments.append(("class:toolbar.cost", f"${state.cost_estimate_total:.4f}"))
    fragments.append(("", sep))

    if _is_active(state):
        # The live cooking affordance (work clock folded in) — class:toolbar.elapsed
        # so the seconds read as the running clock.
        fragments.append(("class:toolbar.elapsed", cooking_text(state, spinner)))
    else:
        status_word = f"{spinner}{state.status}".strip()
        fragments.append((_status_class(state.status), status_word))

    return FormattedText(fragments)
