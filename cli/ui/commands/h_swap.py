"""``/swap`` in the REPL — the twin of ``surfaces/telegram/swap_ops.py``.

Registered as the ``repl`` handler of the contributed row in
``core.money_verbs``. Same parse, same quote-then-confirm, same sentences.
"""
from __future__ import annotations


async def h_swap(ctx) -> None:
    """Quote a swap (a card); ``go`` swaps it."""
    from cli.ui.commands.h_owner import _tenant
    from surfaces.telegram.swap_ops import swap_reply
    ctx.emit(await swap_reply(_tenant(ctx), list(ctx.args or [])), title="swap")
