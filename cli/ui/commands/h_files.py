"""h_files.py — ``/files`` in the REPL (043 A23).

Recent run artifacts from the episode registry — the owner's file view over
what background runs actually produced. Thin over the SAME
``surfaces.telegram.harness._files_reply`` the phone renders, so "what did the
agent make" reads identically on both seats.

Async: ``_files_reply`` reads the episode registry over an awaitable and the
registry awaits an awaitable handler (``CommandRegistry.dispatch``).
"""
from __future__ import annotations

from cli.ui.commands.h_owner import _tenant


async def h_files(ctx) -> None:
    """Recent artifacts (newest first) produced by this tenant's runs."""
    from surfaces.telegram.harness import _files_reply
    ctx.emit(await _files_reply(_tenant(ctx), list(ctx.args or [])), title="files")


HELP_FILES = (
    "  What background runs actually produced — recent file artifacts from the\n"
    "  episode registry, newest first, deduplicated by path.\n"
    "\n"
    "    /files        the last ~10\n"
    "    /files 30     up to 30 distinct files\n"
    "\n"
    "  A path is a real workspace artifact, not a guess; when nothing has been\n"
    "  recorded it says so rather than showing an empty list as 'done'.",
    "`/files` on Telegram and the console's Work › Apps.",
)


def register(reg, Command) -> None:
    """Register ``/files``. Called from ``h_a23.register``."""
    reg.register(Command(
        "files", h_files,
        "Recent run artifacts (newest first) — what background runs produced",
        usage="[n]", group="look",
        help_long=HELP_FILES[0], elsewhere=HELP_FILES[1],
    ))
