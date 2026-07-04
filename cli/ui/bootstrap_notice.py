"""bootstrap_notice.py — transient 'starting…' line for the REPL boot (bug E).

The REPL prints a ``starting…`` notice before the container builds. On a TTY it
should be TRANSIENT — written without a newline and ERASED once the banner is
ready — so it doesn't linger at the top of the transcript (Claude-Code clean
header). On a non-TTY / ``NO_COLOR`` stream it degrades to a plain line: no ANSI
erase sequence (which would print as literal junk into a pipe).

Both helpers take an explicit stream so the boot path can capture the REAL stdout
BEFORE the bootstrap-output suppression swaps it, and erase the same stream after.
"""

from __future__ import annotations

import sys
from typing import Any

from cli.ui.theme import is_tty, no_color

_NOTICE = "starting…"


def show_start_notice(stream: Any = None) -> bool:
    """Write the ``starting…`` notice; return True iff it was written transiently.

    Transient (TTY + color): no trailing newline, so ``clear_start_notice`` can
    erase it in place. Otherwise a plain newline-terminated line.
    """
    stream = stream if stream is not None else sys.stdout
    transient = is_tty(stream) and not no_color()
    stream.write(_NOTICE if transient else _NOTICE + "\n")
    try:
        stream.flush()
    except Exception:
        pass
    return transient


def clear_start_notice(stream: Any, transient: bool) -> None:
    """Erase a transient notice (carriage-return + clear-to-EOL). No-op otherwise."""
    if not transient:
        return
    stream.write("\r\x1b[K")
    try:
        stream.flush()
    except Exception:
        pass
