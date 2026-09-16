"""h_prefs.py — ``/prefs`` in the REPL (043 A23).

Read-only resolved-preferences summary via the display SSOT
(``core.prefs.display_effective``) — never the raw file, always the effective
(pref/env/merged) value plus its source. Thin over the SAME
``surfaces.telegram.harness._prefs_reply`` the phone renders, so the two seats
agree on what is set and where it came from.

Bare ``/prefs`` shows only what is actually SET; ``/prefs all`` (like
``/config list``) shows the whole schema. Writes stay on ``/config set`` — a
guarded change arrives as a ``/pending`` proposal, never a silent write.
"""
from __future__ import annotations

from cli.ui.commands.h_owner import _admin_data_dir, _tenant


def h_prefs(ctx) -> None:
    """Read-only effective-preferences summary (value + source)."""
    from core.instance import resolve_instance_id
    from surfaces.telegram.harness import _prefs_reply
    args = list(ctx.args or [])
    full = bool(args) and args[0].lower() in ("all", "full")
    ctx.emit(
        _prefs_reply(_tenant(ctx), _admin_data_dir(ctx), resolve_instance_id(), full=full),
        title="prefs",
    )


HELP_PREFS = (
    "  Your resolved preferences — the effective value and where it came from\n"
    "  (a pref you set, an env flag, or the built-in default), never the raw\n"
    "  file.\n"
    "\n"
    "    /prefs        only what is actually SET\n"
    "    /prefs all    the whole schema, on defaults included\n"
    "\n"
    "  Read-only. Change one with /config set <key> <value>; a guarded key\n"
    "  arrives as a /pending proposal rather than a silent write.",
    "`/prefs` on Telegram, `/config` here, and the console's Agent › Settings.",
)


def register(reg, Command) -> None:
    """Register ``/prefs``. Called from ``h_a23.register``."""
    reg.register(Command(
        "prefs", h_prefs,
        "Read-only resolved preferences: effective value + source",
        usage="[all]", group="set up",
        help_long=HELP_PREFS[0], elsewhere=HELP_PREFS[1],
    ))
