"""h_mode.py — ``/mode`` in the REPL (043 A23).

Just the effective-posture card + how to change it (read-only). ``/status``
already embeds the card inside a long snapshot; ``/mode`` answers the single
question "what may this instance do right now, and why" with nothing else
around it. Thin over the SAME ``surfaces.telegram.harness._mode_reply`` — which
renders ``core.config_policy.posture_card`` — so the card reads identically on
every seat. Writes stay on the CLI seat (mode/posture are env flags).
"""
from __future__ import annotations


def h_mode(ctx) -> None:
    """Render the effective-posture card (all axes), read-only."""
    from surfaces.telegram.harness import _mode_reply
    ctx.emit(_mode_reply(), title="mode")


HELP_MODE = (
    "  What this instance may do right now, and why — the effective posture\n"
    "  across every axis (trust, autonomy loops, compute, autonomy mode), read\n"
    "  from the resolved flags, not a promise.\n"
    "\n"
    "  Read-only here. Mode and posture are env flags; /status embeds this same\n"
    "  card inside its longer snapshot.",
    "`/mode` on Telegram, `polyrob autonomy status`, and the console's Agent › Overview.",
)


def register(reg, Command) -> None:
    """Register ``/mode``. Called from ``h_a23.register``."""
    reg.register(Command(
        "mode", h_mode,
        "The effective-posture card: what this instance may do right now, and why",
        group="look",
        help_long=HELP_MODE[0], elsewhere=HELP_MODE[1],
    ))
