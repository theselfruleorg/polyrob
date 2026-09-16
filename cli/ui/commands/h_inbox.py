"""h_inbox.py — ``/inbox`` and ``/book`` in the REPL (043 D1).

Its own module rather than a pair of handlers in ``handlers.py``: that file is
at its size ratchet (``tests/test_file_size_ratchet.py`` — extract new behaviour
into a new module, never grow the god-file), the same call ``h_help.py`` and
``h_token.py`` already made.

Both verbs are thin: the Inbox is composed by :mod:`core.surfaces.inbox` over
:mod:`surfaces.inbox_sources`, the book by ``tools.defi.book.read_book``, and
both are rendered by :mod:`core.surfaces.inbox_render` — the same renderer
Telegram and ``polyrob wallet book`` use, so the terminal and the phone cannot
start describing one list differently.

``/inbox [n]`` shows the first *n* of each section; bare, it shows them all.
"""
from __future__ import annotations

import logging

from cli.ui.commands.registry import CommandContext

logger = logging.getLogger(__name__)


def _home(ctx: CommandContext) -> str:
    """The data home this REPL is bound to — the container's, never the
    process default, so a ``-P`` profile's Inbox is that profile's."""
    from core.runtime_paths import data_dir_or_home
    cfg = getattr(ctx.container, "config", None) if ctx.container else None
    return data_dir_or_home(getattr(cfg, "data_dir", None))


def _limit(ctx: CommandContext):
    for arg in (ctx.args or []):
        try:
            return max(1, int(arg))
        except (TypeError, ValueError):
            continue
    return None


def build(user_id: str, home_dir: str) -> dict:
    """The composed Inbox. Kept as a named seam so a test replaces the stores
    rather than the rendering."""
    from surfaces.inbox_sources import build_inbox
    return build_inbox(user_id, data_dir=home_dir)


def h_inbox(ctx: CommandContext) -> None:
    """What is waiting on you, blocking first — and which lists refused."""
    from core.surfaces.inbox_render import render_inbox

    user_id = ctx.user_id or "local"
    try:
        body = build(user_id, _home(ctx))
    except Exception as exc:  # the composer itself refused: say so, never zero
        logger.debug("inbox build failed", exc_info=True)
        ctx.emit(f"  I could not read the inbox at all ({exc}). That is UNKNOWN,\n"
                 f"  not 'nothing needs you'.", title="inbox")
        return
    ctx.emit(render_inbox(body, limit=_limit(ctx)), title="inbox")


async def h_book(ctx: CommandContext) -> None:
    """The ledger against every money chain: one verdict, then what disagrees.

    ``async`` on purpose: ``read_book`` reads every money chain over the
    network, and the registry awaits an awaitable handler
    (``CommandRegistry.dispatch``). Bridging it back to sync would block the
    REPL's own loop for the duration of those reads.
    """
    from core.surfaces.inbox_render import render_book
    from tools.defi.book import read_book

    user_id = ctx.user_id or "local"
    try:
        body = await read_book(user_id, _home(ctx))
    except Exception as exc:
        logger.debug("book read failed", exc_info=True)
        ctx.emit(f"  I could not read the book ({exc}). That is UNKNOWN, not a\n"
                 f"  clean book — do not trade on it.", title="book")
        return
    ctx.emit(render_book(body), title="book")


#: ``/help <verb>`` bodies. Authored here beside the verbs rather than in
#: h_help.py's table, which is the CLI's own long-form home for the older set.
HELP_INBOX = (
    "  Everything that is waiting on a decision from you, in one list,\n"
    "  blocking first. I keep working on everything else while these wait.\n"
    "\n"
    "  The number is DECISIONS. Something listed under \"not blocking\" is\n"
    "  there because you may want to act, not because I am stuck.\n"
    "\n"
    "  If a list refuses to open I say so and mark the count as a floor. I\n"
    "  never tell you nothing needs you over a list I could not read.\n"
    "\n"
    "    /inbox         everything waiting\n"
    "    /inbox 5       the first five of each section",
    "`/inbox` on Telegram (`/pending` is still there), and the console's Inbox.",
)

HELP_BOOK = (
    "  What I have written down against what the chains actually hold, on\n"
    "  every money chain at once.\n"
    "\n"
    "  A disagreement is not an error to dismiss: until it is settled I will\n"
    "  not trade, rewrite the ledger, or say anything in public about my\n"
    "  positions. A chain I could not read is UNKNOWN, never clean.",
    "`/book` on Telegram, `polyrob wallet book`, and the console's Money.",
)


def register(reg, command_cls) -> None:
    """Add both verbs to *reg*. Called from ``handlers.build_default_registry``."""
    reg.register(command_cls(
        "inbox", h_inbox,
        "What is waiting on a decision from you, blocking first",
        usage="[n]", group="needs you",
        help_long=HELP_INBOX[0], elsewhere=HELP_INBOX[1],
    ))
    reg.register(command_cls(
        "book", h_book,
        "The ledger against every money chain: one verdict, then what disagrees",
        group="money",
        help_long=HELP_BOOK[0], elsewhere=HELP_BOOK[1],
    ))
