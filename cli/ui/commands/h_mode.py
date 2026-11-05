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
    """The four answers (104 §2); `/mode all` adds every setting behind them."""
    from core.permission_levels import LEVELS, level_reply
    from surfaces.telegram.harness import _mode_reply
    if ctx.args and ctx.args[0] in LEVELS:
        ctx.emit(_wrap(level_reply(ctx.args[0], surface="local")), title="mode")
        return
    full = "all" in (ctx.args or ())
    ctx.emit(_wrap(_mode_reply("terminal", full=full)), title="mode")


HELP_MODE = (
    "  Who you are talking to, what the agent may do without asking, what it does\n"
    "  when you are away, and how to change that. Read from the resolved settings,\n"
    "  not a promise.\n"
    "\n"
    "  /mode all adds every setting behind the answers. /mode ask-first, /mode standard\n"
    "  and /mode on-my-own set one level, and never touch money or the computer.",
    "`/mode` on Telegram, `polyrob autonomy status`, and the console's Agent › Overview.",
)


def register(reg, Command) -> None:
    """Register ``/mode``. Called from ``h_a23.register``."""
    reg.register(Command(
        "mode", h_mode,
        "What the agent may do without you, and how to change it",
        group="control",
        help_long=HELP_MODE[0], elsewhere=HELP_MODE[1],
    ))
