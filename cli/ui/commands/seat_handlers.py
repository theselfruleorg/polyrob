"""The REPL seat's handlers for contributed owner verbs (proposal 125 S14).

``core.money_verbs`` contributes the rows and names no seat module (core may not
reference a seat); this module attaches the REPL's handlers through
:func:`core.verbs.register_seat_handlers`. ``h_contributed`` and
``polyrob wallet send|swap|check`` call :func:`ensure` (idempotent) before they
look a handler up.
"""
from core import verbs as _verbs

REPL_HANDLERS = {
    "/send": "cli.ui.commands.h_send:h_send",
    "/swap": "cli.ui.commands.h_swap:h_swap",
    "/check": "cli.ui.commands.h_check:h_check",
    "/writeoff": "cli.ui.commands.h_holdings:h_writeoff",
    "/unquarantine": "cli.ui.commands.h_holdings:h_unquarantine",
}



def ensure() -> None:
    """Attach the REPL's handlers (idempotent: the same references are a no-op)."""
    _verbs.register_seat_handlers("repl", REPL_HANDLERS, source="cli.ui.commands")


ensure()

__all__ = ["REPL_HANDLERS", "ensure"]
