"""h_mode.py — ``/mode`` in the REPL (043 A23).

Just the effective-posture card + how to change it (read-only). ``/status``
already embeds the card inside a long snapshot; ``/mode`` answers the single
question "what may this instance do right now, and why" with nothing else
around it. Thin over the SAME ``surfaces.telegram.harness._mode_reply`` — which
renders ``core.config_policy.posture_card`` — so the card reads identically on
every seat. Writes stay on the CLI seat (mode/posture are env flags).
"""
from __future__ import annotations


def _wrap(text: str) -> str:
    """Fold the card to the terminal's own width (C41).

    The card is authored for a chat bubble, so several of its lines run past
    80 columns and a narrow terminal hard-wrapped them mid-word, breaking the
    alignment the card uses to say which axis a value belongs to. ``h_help``'s
    :func:`_width` is the ONE width the REPL reads, so /mode and /help fold at
    the same column. Indentation is preserved; a blank line stays blank.
    """
    import textwrap

    from cli.ui.commands.h_help import _width

    width = _width()
    out = []
    for line in text.splitlines():
        if len(line) <= width:
            out.append(line)
            continue
        indent = line[:len(line) - len(line.lstrip())]
        out.extend(textwrap.wrap(
            line, width=width, initial_indent="", subsequent_indent=indent + "  ",
            break_long_words=False, break_on_hyphens=False) or [line])
    return "\n".join(out)


def h_mode(ctx) -> None:
    """Render the effective-posture card (all axes), read-only."""
    from surfaces.telegram.harness import _mode_reply
    ctx.emit(_wrap(_mode_reply()), title="mode")


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
        group="control",
        help_long=HELP_MODE[0], elsewhere=HELP_MODE[1],
    ))
