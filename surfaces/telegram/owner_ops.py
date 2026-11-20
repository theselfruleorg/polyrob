"""The chat seats' own owner replies that no other seat shares: `/inbox` laid out for a
phone, `/book` and `/mcp` (G13).

The bodies of the other owner verbs live below the seats — `/goal` in
``cron/owner_goals.py``, `/wallet`, `/invoices`, `/settle` and `/bridge` in
``tools/defi/owner_verbs/``, `/avatar` in ``tools/avatar_owner.py`` — and the seat
table (``surfaces/telegram/admin_verbs.py``) imports each from there.

Honest about what will actually happen: an unreadable store is UNKNOWN, never
"nothing needs you".
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


async def mcp_reply(user_id: Optional[str], args: List[str]) -> str:
    """`/mcp [add <id> <url> [key] | remove <id> | test <id>]`.

    Thin plumbing over ``core.mcp_admin`` — the ONE helper set every owner seat
    renders, so Telegram, the REPL and the console can never drift into three
    different answers about which MCP servers exist. AWAITED on the seat's loop
    (the one that owns the live MCP manager); never bridged onto another loop.
    """
    from core import mcp_admin
    return await mcp_admin.mcp_reply_async(user_id, args)


_CHAT_WIDTH = 60
#: Rows per section before the reply becomes a scroll rather than a decision.
_CHAT_ITEMS = 5


def _inbox_limit(args: Optional[List[str]]) -> Tuple[Optional[int], Optional[str]]:
    """``(limit, error)`` for ``/inbox [all|n]``: no word = the phone default,
    ``all`` = every item, ``n`` = n per section."""
    if not args:
        return _CHAT_ITEMS, None
    word = str(args[0]).strip().lower()
    if word == "all":
        return None, None
    if word.isdigit() and int(word) > 0:
        return int(word), None
    return None, "Usage: /inbox [all|n] — `all` lists everything, `n` that many per section."


def inbox_reply(user_id: str, data_dir: str, args: Optional[List[str]] = None) -> str:
    """Everything waiting on an owner decision, blocking first.

    ``/inbox`` shows :data:`_CHAT_ITEMS` per section and keeps the rest one tap
    away (``/more_<token>``); ``/inbox all`` lists everything, ``/inbox 12``
    twelve per section (096 C6: ``[n]`` was advertised and ignored).

    The SAME composition the console and the REPL render
    (``core.surfaces.inbox`` over ``surfaces.inbox_sources``), through the SAME
    text renderer (``core.surfaces.inbox_render``) with this seat's own remedy
    verbs — so three seats can never disagree about what is waiting.

    ⚠️ A composer failure is reported as UNKNOWN, never as "nothing needs you".
    """
    from core.surfaces.inbox_render import CHAT_REMEDIES, render_inbox
    from surfaces.inbox_sources import build_inbox
    limit, err = _inbox_limit(args)
    if err:
        return err
    try:
        body = build_inbox(user_id, data_dir=data_dir)
    except Exception as exc:
        logger.warning("inbox: composition failed", exc_info=True)
        return (f"I could not read the inbox at all ({exc}). That is UNKNOWN, "
                f"not 'nothing needs you'.")
    return phone_layout(render_inbox(body, remedies=CHAT_REMEDIES, limit=limit,
                                     width=_PHONE_NO_WRAP, more_tenant=user_id))


#: A width no inbox line reaches: a phone wraps the text itself (2026-11-20 UX review M11).
_PHONE_NO_WRAP = 10_000
_PAD_RE = __import__("re").compile(r"(?<=\S) {3,}(?=\S)")


def phone_layout(text: str) -> str:
    """A terminal-shaped render for a phone: no two-space margin, and a right-aligned
    column (an age) joined with `` · `` instead of padding. Deeper indents keep their
    nesting, two spaces less."""
    out = []
    for line in str(text or "").splitlines():
        line = line[2:] if line.startswith("  ") else line
        out.append(_PAD_RE.sub(" · ", line))
    return "\n".join(out)


async def book_reply(user_id: str, data_dir: str) -> str:
    """The ledger against every money chain — one verdict, then what disagrees."""
    from core.surfaces.inbox_render import render_book
    from tools.defi.book import read_book
    try:
        body = await read_book(user_id, data_dir)
    except Exception as exc:
        logger.warning("book: read failed", exc_info=True)
        return (f"I could not read the book ({exc}). That is UNKNOWN, not a "
                f"clean book — do not trade on it.")
    return render_book(body, width=_CHAT_WIDTH)
