"""The Telegram seat's handlers for contributed owner verbs (proposal 125 S14).

``core.money_verbs`` contributes the rows and names no seat module (core may not
reference a seat); this module attaches Telegram's handlers through
:func:`core.verbs.register_seat_handlers`. The harness reads :func:`handler_ref`
from here, which registers the table (idempotent) before every lookup.
"""
from typing import Optional

from core import verbs as _verbs

TELEGRAM_HANDLERS = {
    "/send": "surfaces.telegram.send_ops:send_verb",
    "/swap": "surfaces.telegram.swap_ops:swap_verb",
    "/check": "surfaces.telegram.check_ops:check_verb",
    "/writeoff": "surfaces.telegram.holdings_ops:writeoff_verb",
    "/unquarantine": "surfaces.telegram.holdings_ops:unquarantine_verb",
}



def ensure() -> None:
    """Attach Telegram's handlers (idempotent: the same references are a no-op)."""
    _verbs.register_seat_handlers("telegram", TELEGRAM_HANDLERS, source="surfaces.telegram")


def handler_ref(seat: str, name: str) -> Optional[str]:
    """``core.verbs.handler_ref`` with this seat's table registered first."""
    ensure()
    return _verbs.handler_ref(seat, name)


ensure()

__all__ = ["TELEGRAM_HANDLERS", "ensure", "handler_ref"]
