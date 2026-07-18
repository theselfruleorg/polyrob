"""candy.py — tiny shared plain-text grammar helpers for slash-command views.

NOT a rendering layer: every helper returns a plain ``str`` that flows through
the EXISTING output paths (``ctx.emit(text, title=…)`` → RichRenderer Panel /
PlainRenderer ``--- title ---``). The Rich-Table views keep their existing
dual-path idiom. These helpers give every view ONE gutter, ONE table
alignment, ONE empty-state grammar, ONE status-glyph vocabulary
(``theme.state_glyph``) and ONE section idiom, so the whole slash-command
surface reads as designed, not assembled.

Scrubbing stays where it already lives (``ctx.emit`` / ``_print_scrubbed``);
candy adds no scrub layer, no I/O, no Rich objects.
"""

from __future__ import annotations

from typing import Any, List, Sequence, Tuple

from cli.ui.theme import ICONS, state_glyph

#: The one left gutter every view body uses.
GUTTER = "  "


def kv_lines(rows: Sequence[Tuple[str, Any]]) -> str:
    """Aligned ``label  value`` rows with the shared 2-space gutter."""
    clean = [(str(k), str(v)) for k, v in rows]
    width = max((len(k) for k, _ in clean), default=0)
    return "\n".join(f"{GUTTER}{k:<{width}}  {v}" for k, v in clean)


def table_lines(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Aligned plain grid (header + rows); short rows padded with ""."""
    cols = [str(c) for c in columns]
    cells: List[List[str]] = []
    for row in rows:
        r = [str(c) for c in row][: len(cols)]
        r += [""] * (len(cols) - len(r))
        cells.append(r)
    widths = [max([len(cols[i])] + [len(r[i]) for r in cells]) for i in range(len(cols))]
    lines = [GUTTER + "  ".join(f"{cols[i]:<{widths[i]}}" for i in range(len(cols))).rstrip()]
    for r in cells:
        lines.append(GUTTER + "  ".join(f"{r[i]:<{widths[i]}}" for i in range(len(cols))).rstrip())
    return "\n".join(lines)


def status_line(state_word: str, text: str) -> str:
    """One row led by the shared state-glyph vocabulary: ``  ● text``."""
    glyph, _role = state_glyph(state_word)
    return f"{GUTTER}{glyph} {text}"


def bullet(text: str) -> str:
    """``  · text`` — the one bullet gutter (no per-view ``-``/``•``/emoji)."""
    return f"{GUTTER}{ICONS.bullet} {text}"


def empty(what: str, hint: str = "", *, yet: bool = True) -> str:
    """The one empty-state grammar: ``  no {what} yet — {hint}``."""
    msg = f"{GUTTER}no {what}" + (" yet" if yet else "")
    return f"{msg} — {hint}" if hint else msg


def section(title: str) -> str:
    """One inner-section idiom: ``── title`` (callers blank-line before it)."""
    return f"── {title}"
