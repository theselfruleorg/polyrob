"""terminal_title.py — set/clear the terminal tab & window title.

A ``polyrob`` launch shows the interpreter's process name ("Python") in the
terminal tab, because the entry point is a python script and nothing ever set a
title. Emitting the standard OSC 0 escape sequence (``ESC ] 0 ; title BEL``)
names the tab like other CLIs do (e.g. Claude Code's version tab title); it is
honored by Terminal.app, iTerm2, VS Code, and every xterm-compatible emulator.

Fail-open by design: only writes to a real TTY that is not ``TERM=dumb``, and
never raises — a title is cosmetic, it must not break a boot or an exit path.
Callers gate ``--plain``/``POLYROB_PLAIN`` themselves (render-mode concern).
"""

from __future__ import annotations

import os
import sys
from typing import Any

from cli.ui.theme import is_tty


def _title_capable(stream: Any) -> bool:
    return is_tty(stream) and os.environ.get("TERM", "").lower() != "dumb"


def set_terminal_title(text: str, stream: Any = None) -> bool:
    """Set the tab/window title. Return True iff the sequence was written."""
    stream = stream if stream is not None else sys.stdout
    if not _title_capable(stream):
        return False
    try:
        stream.write(f"\x1b]0;{text}\x07")
        stream.flush()
        return True
    except Exception:
        return False


def clear_terminal_title(stream: Any = None) -> None:
    """Reset the title to empty so the terminal falls back to its default."""
    stream = stream if stream is not None else sys.stdout
    if not _title_capable(stream):
        return
    try:
        stream.write("\x1b]0;\x07")
        stream.flush()
    except Exception:
        pass
