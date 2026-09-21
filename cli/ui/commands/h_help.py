"""h_help.py — grouped ``/help`` + ``/help <verb>`` (043 A13/A24).

Lives in its own module rather than growing ``handlers.py``: that file is at
its size ratchet (``tests/test_file_size_ratchet.py`` — extract new behaviour
into a new module, don't grow the god-file), the same call ``h_token.py``
already made for ``/launch``/``/deploy``.

Renders the registry's ``group``/``help_long``/``elsewhere`` metadata
(``cli/ui/commands/registry.py::Command``):

- No argument → the grouped catalog. ``docs/design/040/cli/help-grouped-80.txt``
  is the specification: ten groups (:data:`~cli.ui.commands.registry.GROUP_ORDER`),
  names only, wrapped at the terminal width. Only REGISTERED verbs render —
  the mockup lists a few verbs that don't have a REPL command yet.
- One argument → the verb's own line (``help``/``usage``), then ``help_long``
  if authored (wrapped a line at a time, preserving each line's own
  indentation so an already-fitting block — e.g. the ``/pause`` forms —
  renders unchanged), then ``elsewhere`` if authored. Unknown verb → the
  same "Unknown command" text ``CommandRegistry.dispatch`` prints.

Width is read fresh on every call via ``shutil.get_terminal_size`` (honours
the ``COLUMNS`` env var), floored at 60 so a tiny/piped terminal never
collapses the wrap to nothing.
"""
from __future__ import annotations

import shutil
import textwrap
from typing import Dict, List, Tuple

from cli.ui.commands.registry import CommandContext, CommandRegistry, GROUP_ORDER


def _width() -> int:
    cols = shutil.get_terminal_size((80, 24)).columns
    return max(60, cols)


_FOOTER = (
    "You don't have to use a command. Ask me in plain words — "
    "\"schedule a check every morning at 9\", \"what did you spend this "
    "week\" — and I'll use my own tools."
)


def _grouped_help(reg: CommandRegistry) -> str:
    width = _width()
    by_group: Dict[str, List[str]] = {}
    for cmd in reg.commands():  # canonical, name-sorted (registry.py)
        by_group.setdefault(cmd.group or "other", []).append(cmd.name)

    # Any group outside GROUP_ORDER (an ad-hoc test registry, e.g.) still
    # renders, appended in first-seen order — never a silent drop.
    order = list(GROUP_ORDER) + [g for g in by_group if g not in GROUP_ORDER]

    lines = ["Commands. /help <verb> tells you what one of them does.", ""]
    for group in order:
        names = by_group.get(group)
        if not names:
            continue
        prefix = "  " + f"{group:<12}"
        indent = " " * len(prefix)
        text = " ".join(f"/{n}" for n in names)
        lines.extend(
            textwrap.wrap(
                text,
                width=width,
                initial_indent=prefix,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [prefix.rstrip()]
        )
    lines.append("")
    lines.extend(textwrap.wrap(_FOOTER, width=width))
    return "\n".join(lines)


def _wrap_help_long(text: str, width: int) -> List[str]:
    """Wrap ``help_long`` a line at a time, preserving each line's own
    leading indentation (a paragraph is authored at 2 spaces, a form/example
    list deeper) — only a line that actually overflows *width* gets
    rewrapped, so an authored block that already fits (e.g. the ``/pause``
    forms) renders byte-identical to how it was written."""
    out: List[str] = []
    for raw_line in text.split("\n"):
        content = raw_line.strip()
        if not content:
            out.append("")
            continue
        indent = raw_line[: len(raw_line) - len(raw_line.lstrip(" "))] or "  "
        full = indent + content
        if len(full) <= width:
            out.append(full)
            continue
        out.extend(
            textwrap.wrap(
                content,
                width=width,
                initial_indent=indent,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    return out


def _verb_help(cmd) -> str:
    width = _width()
    usage_part = f" {cmd.usage}" if cmd.usage else ""
    header = f"  /{cmd.name}{usage_part}  — {cmd.help}"
    if len(header) <= width:
        lines = [header]
    else:
        # A verb whose usage/help is long enough to overflow width (R13: every
        # default view fits it) — wrap the header itself, same hanging-indent
        # convention as everything else here.
        lines = textwrap.wrap(
            f"/{cmd.name}{usage_part}  — {cmd.help}",
            width=width,
            initial_indent="  ",
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    if cmd.help_long:
        lines.append("")
        lines.extend(_wrap_help_long(cmd.help_long, width))
    if cmd.elsewhere:
        lines.append("")
        lines.extend(
            textwrap.wrap(
                f"Elsewhere: {cmd.elsewhere}",
                width=width,
                initial_indent="  ",
                subsequent_indent="  ",
            )
        )
    return "\n".join(lines)


def _unknown(verb: str) -> str:
    # Mirrors CommandRegistry.dispatch's own unknown-command text exactly.
    return f"Unknown command: /{verb.lstrip('/')} — type /help"


def h_help(ctx: CommandContext) -> None:
    """``/help`` (grouped catalog) or ``/help <verb>`` (one command's detail),
    with or without the leading slash — aliases resolve (``/help ?``)."""
    reg = ctx.registry or _default_registry()
    args = list(getattr(ctx, "args", None) or [])
    if not args:
        ctx.emit(_grouped_help(reg), title="help")
        return
    cmd = reg.lookup(args[0])
    if cmd is None:
        ctx.emit(_unknown(args[0]), title="help")
        return
    ctx.emit(_verb_help(cmd), title="help")


def _default_registry():
    from cli.ui.commands.handlers import default_registry

    return default_registry()


# ---------------------------------------------------------------------------
# Authored ``help_long``/``elsewhere`` content — the minimum set (A13 brief):
# pause resume halt pending asks fulfill finance invoices settle goals cron
# status doctor missed config model. ``(help_long, elsewhere)`` per verb; a
# verb not in this dict just shows its one-line ``help`` under ``/help <verb>``
# (still an honest answer — R14 asks for SOME answer for every verb, not a
# long-form one for every verb).
#
# ``pause`` is the ``docs/design/040/cli/help-verb-80.txt`` mockup, verbatim —
# including its closing "Lift it with /resume…" sentence — so ``elsewhere``
# stays empty for it rather than splitting that sentence out under a second
# "Elsewhere:" label the mockup doesn't show.
# ---------------------------------------------------------------------------

HELP_TEXT: Dict[str, Tuple[str, str]] = {
    "pause": (
        "  I stop starting new work of my own. Anything already running is\n"
        "  cancelled and put back on the board, so nothing is lost. I still\n"
        "  answer you.\n"
        "\n"
        "    /pause                  everything\n"
        "    /pause trading          only the money verbs\n"
        "    /pause background       only the work I start by myself\n"
        "    /pause messages         only the messages I send you\n"
        "    /pause for 6h           everything, until 6 hours from now\n"
        "\n"
        "  Lift it with /resume. Every seat sees the same pause: this terminal,\n"
        "  Telegram, the app, and polyrob autonomy resume.",
        "",
    ),
    "resume": (
        "  Lifts a pause you or I set. With no word, everything resumes; add\n"
        "  a word to resume only that part while the rest stays paused.\n"
        "\n"
        "    /resume                 everything\n"
        "    /resume trading         only that scope\n"
        "\n"
        "  Saying \"resume\" or \"unpause\" in plain chat does the same when a\n"
        "  pause is on.",
        "the same lift on Telegram, the app, and `polyrob autonomy resume`.",
    ),
    "halt": (
        "  The fast path to a full stop: no words to remember, no scope to\n"
        "  pick. Same effect as /pause with nothing after it — everything\n"
        "  in-flight is cancelled and put back on the board.",
        "the plain word \"stop\" does the same in chat, on Telegram and here.",
    ),
    "pending": (
        "  I sometimes write things I don't apply until you look at them: a\n"
        "  skill I distilled, a note about myself, a preference change. This\n"
        "  is that review queue.\n"
        "\n"
        "    /pending                       list what's waiting\n"
        "    /pending show <kind> <id>      read one in full\n"
        "    /pending approve <kind> <id>   apply it\n"
        "    /pending reject <kind> <id>    archive it (recoverable)",
        "`polyrob owner pending`, and the app's Review page.",
    ),
    "asks": (
        "  Some goals stall on something only you can give me — a decision,\n"
        "  a credential, a yes. Each one becomes an ask here instead of me\n"
        "  guessing or retrying forever.\n"
        "\n"
        "    /asks                    list what's open\n"
        "    /fulfill <id>            mark one done, unblock its goals",
        "`polyrob owner asks`, and Telegram /asks.",
    ),
    "fulfill": (
        "  Marks one open ask as done and puts every goal it was blocking\n"
        "  back to ready. See /asks for the id.",
        "`polyrob owner fulfill <id>`, and Telegram /fulfill.",
    ),
    "finance": (
        "  Two ledgers, never summed: what I've earned and spent as myself\n"
        "  (income, spend, pending, net), and what running me has cost you\n"
        "  in compute. Default window is 30 days.\n"
        "\n"
        "    /finance                the default window\n"
        "    /finance 7               last 7 days",
        "the app's Finance page shows the same two ledgers.",
    ),
    "invoices": (
        "  Payment requests I've sent out, whether or not they're paid yet.\n"
        "\n"
        "    /invoices                every invoice\n"
        "    /invoices pending        only unpaid ones",
        "`polyrob owner invoices`, and Telegram /invoices.",
    ),
    "settle": (
        "  Marks one invoice paid by your word, not a scan — use this when\n"
        "  you saw the payment land somewhere I can't check on my own. See\n"
        "  /invoices for the id.",
        "`polyrob owner settle <id>`, and Telegram /settle.",
    ),
    "goals": (
        "  The standing backlog of work I've queued for myself: what's\n"
        "  ready, running, blocked on an ask, or done.",
        "Telegram /goals, and the app's Autonomy page.",
    ),
    "cron": (
        "  Durable scheduled runs — the work that happens on a clock instead\n"
        "  of a trigger, and survives a restart.",
        "Telegram /cron, and the app's Autonomy page.",
    ),
    "meter": (
        "  This turn only: model, tokens in/out, an estimated cost, how full\n"
        "  the context window is, and how many compactions have run.\n"
        "\n"
        "  A context figure is shown only when the token count and the window\n"
        "  were BOTH read; a percentage beside an unread count says so.",
        "Nothing else carries a per-turn meter — /status is the whole agent.",
    ),
    "status": (
        "  The whole agent, not this turn: what is wrong first, then running\n"
        "  or paused, what needs you, the goals, the loops and the wallet.\n"
        "  The same snapshot /doctor shows.",
        "`/status` on Telegram, `polyrob doctor`, and the console's Agent.",
    ),
    "doctor": (
        "  What is wrong first, then what I am: the ranked health lines, then\n"
        "  the snapshot — running or paused, what needs you, the goals and\n"
        "  loops, what the last day cost. Nothing checked reads as OK by\n"
        "  omission; a source I could not read says so.\n"
        "\n"
        "  The long form — every provider, flag and database check a fresh\n"
        "  install runs — is `polyrob doctor` on the command line.",
        "`polyrob doctor` on the command line, and the app's System page.",
    ),
    "missed": (
        "  What I tried to tell you but couldn't send live: capped by the\n"
        "  daily limit, held by a pause, or a delivery that failed outright.\n"
        "  Nothing here was ever lost — only delayed until you ask.\n"
        "\n"
        "    /missed                  last 5, newest first\n"
        "    /missed 20               last 20",
        "`polyrob owner missed`, and Telegram /missed.",
    ),
    "config": (
        "  Everything you can tune, in one place: plain preferences and the\n"
        "  full catalog underneath them.\n"
        "\n"
        "    /config                  opens a picker\n"
        "    /config list             every setting, grouped\n"
        "    /config get <key>        one setting's current value\n"
        "    /config set <key> <val>  change it\n"
        "    /config explain <key>    what it does and why",
        "`polyrob config`, and the app's Config page.",
    ),
    "model": (
        "  Changes the model for this running session right away, and saves\n"
        "  it as the default for the next one you start.\n"
        "\n"
        "    /model                    opens a picker\n"
        "    /model <provider> <model>\n"
        "    /model <provider>/<model>",
        "`polyrob model` on the command line.",
    ),
}


def help_kwargs(name: str) -> dict:
    """``Command(...)`` kwargs for an authored verb — spread with ``**``.

    Empty for anything not in :data:`HELP_TEXT`, so
    ``Command(..., group="x", **help_kwargs("y"))`` is always safe — an
    unauthored verb just gets the ``Command`` field defaults (``""``).
    """
    help_long, elsewhere = HELP_TEXT.get(name, ("", ""))
    return {"help_long": help_long, "elsewhere": elsewhere}
