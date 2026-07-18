"""slash_highlight.py — live syntax highlighting for slash commands (REPL input).

A prompt_toolkit ``Processor`` that colors the leading ``/command`` token while
the user types: a known command reads accent, a strict prefix reads neutral,
an unknown one reads warn, and the args after the first space get their own
quiet style. Non-slash input passes through untouched (zero-cost fast path).

Pure classifier (``classify_slash``) + thin Processor adapter. Style classes
(``prompt.slash.*``) live in ``cli.ui.app.toolbar_style`` — the single style
source. The registry binding is lazy and fail-open: if the command registry
can't import, highlighting silently disables (never crashes a keystroke).

Only imported from ``build_app`` (persistent REPL path), so plain/non-TTY
paths never load prompt_toolkit through this module.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.layout.utils import explode_text_fragments

CLASS_VALID = "class:prompt.slash.valid"
CLASS_PARTIAL = "class:prompt.slash.partial"
CLASS_UNKNOWN = "class:prompt.slash.unknown"
CLASS_ARG = "class:prompt.slash.arg"


def classify_slash(
    text: str,
    is_known: Callable[[str], bool],
    is_prefix: Callable[[str], bool],
) -> List[Tuple[int, int, str]]:
    """Return ``(start, end, style_class)`` spans for one input line.

    Empty list when *text* is not a slash line (the fast path).
    """
    if not text.startswith("/"):
        return []
    head, sep, rest = text.partition(" ")
    word = head[1:].lower()
    if not word:
        cls = CLASS_PARTIAL
    elif is_known(word):
        cls = CLASS_VALID
    elif is_prefix(word):
        cls = CLASS_PARTIAL
    else:
        cls = CLASS_UNKNOWN
    spans = [(0, len(head), cls)]
    if sep and rest:
        spans.append((len(head) + 1, len(text), CLASS_ARG))
    return spans


class SlashHighlightProcessor(Processor):
    """Apply ``classify_slash`` spans to the input line's fragments.

    ``gate`` (returns True → suppress) covers the /model picker, which borrows
    the input buffer as its search field. Highlighting applies to line 0 only
    (a ⌥⏎ multiline continuation is never a command).
    """

    def __init__(
        self,
        *,
        gate: Optional[Callable[[], bool]] = None,
        is_known: Optional[Callable[[str], bool]] = None,
        is_prefix: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self._gate = gate
        self._is_known = is_known
        self._is_prefix = is_prefix

    def _bind(self) -> bool:
        if self._is_known is not None and self._is_prefix is not None:
            return True
        try:
            from cli.ui.commands.handlers import default_registry

            reg = default_registry()
            self._is_known = lambda w: reg.lookup(w) is not None
            self._is_prefix = lambda w: any(n.startswith(w) for n in reg.names())
            return True
        except Exception:
            return False

    def apply_transformation(self, ti) -> Transformation:
        try:
            if getattr(ti, "lineno", 0) != 0:
                return Transformation(ti.fragments)
            text = ti.document.lines[0] if ti.document.lines else ""
            if not text.startswith("/"):
                return Transformation(ti.fragments)
            if self._gate is not None and self._gate():
                return Transformation(ti.fragments)
            if not self._bind():
                return Transformation(ti.fragments)
            spans = classify_slash(text, self._is_known, self._is_prefix)
            if not spans:
                return Transformation(ti.fragments)
            frags = explode_text_fragments(list(ti.fragments))
            for start, end, cls in spans:
                for i in range(start, min(end, len(frags))):
                    frag = frags[i]
                    frags[i] = (f"{frag[0]} {cls}".strip(), frag[1], *frag[2:])
            return Transformation(frags)
        except Exception:  # a keystroke must never crash on a styling bug
            return Transformation(ti.fragments)
