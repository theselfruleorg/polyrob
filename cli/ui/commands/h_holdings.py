"""``/writeoff`` and ``/unquarantine`` in the REPL (W1).

The REPL twins of ``surfaces/telegram/holdings_ops.py`` — registered as the
``repl`` handlers of the contributed rows in ``core.money_verbs`` (the
``h_contributed`` registrar adds them). Same parse, same confirm step, same
sentences: ``core.wallet.token_trust``.
"""
from __future__ import annotations


def _positions_db():
    from cli.ui.commands.h_owner import _admin_data_dir
    from core.open_positions import open_positions_db_path
    return open_positions_db_path(_admin_data_dir(write=None))


async def h_writeoff(ctx) -> None:
    """Write a holding off: bare = what it would do; ``go`` records it."""
    from cli.ui.commands.h_owner import _tenant
    from core.wallet.token_trust import writeoff_reply
    ctx.emit(writeoff_reply(_tenant(ctx), list(ctx.args or []),
                            positions_db=_positions_db()), title="writeoff")


async def h_unquarantine(ctx) -> None:
    """Undo a quarantine: bare = what it would do; ``go`` lifts it."""
    from cli.ui.commands.h_owner import _tenant
    from core.wallet.token_trust import unquarantine_reply
    ctx.emit(unquarantine_reply(_tenant(ctx), list(ctx.args or []),
                                positions_db=_positions_db()), title="unquarantine")
