"""Theme — colors, icons, and box styles for the POLYROB CLI Rich renderer (Phase 2).

Single source of truth for the visual vocabulary of the Rich renderer:
glyphs (icons), Rich style strings, and box styles.  Honours ``NO_COLOR``
(https://no-color.org/) and detects truecolor support so the renderer can
degrade gracefully on poorer terminals.

No I/O beyond reading environment variables; no Rich rendering here — this
module only provides the constants and small predicates ``blocks.py`` /
``statusbar.py`` consume.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------


def no_color() -> bool:
    """True when ANSI color must be suppressed (``NO_COLOR`` set, or dumb term)."""
    if os.environ.get("NO_COLOR"):
        return True
    if os.environ.get("TERM", "").lower() == "dumb":
        return True
    return False


def supports_truecolor() -> bool:
    """Best-effort detection of 24-bit truecolor support."""
    ct = os.environ.get("COLORTERM", "").lower()
    return ct in ("truecolor", "24bit")


def is_tty(stream: object | None = None) -> bool:
    """True when *stream* (default stdout) is an interactive terminal."""
    s = stream if stream is not None else sys.stdout
    try:
        return bool(s.isatty())  # type: ignore[attr-defined]
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Icons / glyphs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Icons:
    """Unicode glyphs used across blocks + status bar."""

    step: str = "▸"          # ▸
    arrow: str = "→"         # →
    ok: str = "✓"            # ✓
    fail: str = "✗"          # ✗
    up: str = "↑"            # ↑
    down: str = "↓"          # ↓
    bullet: str = "·"        # ·
    subagent: str = "+"           # collapsed sub-agent line prefix
    error: str = "⚠"         # ⚠
    caret: str = "❯"         # user-turn echo caret


ICONS = Icons()

#: Frames for a lightweight braille spinner (status bar "thinking" indicator).
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴",
                  "⠦", "⠧", "⠇", "⠏")


# ---------------------------------------------------------------------------
# Style strings (Rich markup)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Styles:
    """Rich style strings keyed by semantic role.

    When ``no_color()`` is True these collapse to empty strings so Rich emits
    no ANSI; the renderer should still construct a ``Console`` with
    ``no_color=True`` as the primary guard.
    """

    step_header: str = "bold cyan"
    meta: str = "dim"
    reasoning_border: str = "dim"
    reasoning_text: str = "dim italic"
    memory: str = "dim"
    tool_call: str = "white"
    tool_name: str = "bold"
    tool_arg_path: str = "cyan"
    tool_arg_str: str = "green"
    tool_ok: str = "green"
    tool_fail: str = "red"
    answer: str = "bold white"
    answer_border: str = "green"
    speaker_dot: str = "bold green"
    speaker_name: str = "bold"
    user_caret: str = "bold cyan"
    user_text: str = "bold"
    summary_border: str = "green"
    summary_fail_border: str = "red"
    error_border: str = "red"
    error_text: str = "red"
    subagent: str = "dim"
    status_ok: str = "green"
    status_running: str = "yellow"
    status_error: str = "red"


STYLES = Styles()


def style(role: str) -> str:
    """Return the Rich style string for *role*, or "" when color is disabled."""
    if no_color():
        return ""
    return getattr(STYLES, role, "")


def fmt_tokens(n: int) -> str:
    """Compact token count: 1234 → ``1.2k``."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)
