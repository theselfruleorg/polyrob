"""hints.py — the context-aware hint row under the persistent REPL input.

Pure builder (5 Hz repaint-safe: string ops + a clock-bucket index, no I/O).
Three modes:
- mid-turn  → ``^C stop · ⌥⏎ newline`` (the only actions that matter then)
- idle + a known ``/cmd`` in the buffer → that command's usage line (palette feel)
- idle      → the key hints + one gentle rotating tip

Style classes (``prompt.hint``/``prompt.hint.tip``) live in app.toolbar_style.
No prompt_toolkit import — returns plain (style, text) tuples.
"""

from __future__ import annotations

from typing import Any, List, Tuple

from cli.ui.theme import ICONS

#: Gentle rotating tips (idle only). Keep short; one shows at a time.
TIPS: Tuple[str, ...] = (
    "/model swaps the model live",
    "/goals shows the goal board",
    "Ctrl-L repaints the screen",
    "/recap replays the journey",
    "/skills lists loadable skills",
    "/finance shows earn/spend",
)

_TIP_PERIOD_S = 12.0
_SEP = f"  {ICONS.bullet}  "


def _usage_for(word: str) -> str:
    """`/name usage — help` for a known command, else "". Lazy + fail-open."""
    try:
        from cli.ui.commands.handlers import default_registry

        cmd = default_registry().lookup(word)
        if cmd is None:
            return ""
        usage = f" {cmd.usage}" if cmd.usage else ""
        return f"/{cmd.name}{usage} — {cmd.help}" if cmd.help else f"/{cmd.name}{usage}"
    except Exception:
        return ""


def hint_fragments(state: Any, buffer_text: str, clock_now: float) -> List[Tuple[str, str]]:
    """Build the hint row fragments for the current context."""
    lifecycle = getattr(state, "lifecycle", None)
    if lifecycle is not None and lifecycle.is_active():
        return [("class:prompt.hint", f" ^C stop{_SEP}⌥⏎ newline")]

    text = (buffer_text or "").strip()
    if text.startswith("/") and len(text) > 1:
        word = text[1:].split(" ", 1)[0].lower()
        usage = _usage_for(word)
        if usage:
            return [("class:prompt.hint", f" {usage}")]

    tip = TIPS[int(clock_now / _TIP_PERIOD_S) % len(TIPS)]
    return [
        ("class:prompt.hint", f" ⏎ send{_SEP}⇥ commands{_SEP}/help{_SEP}"),
        ("class:prompt.hint.tip", tip),
    ]
