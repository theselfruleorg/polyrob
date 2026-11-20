"""``/swap`` in the REPL — the seat half of ``tools.defi.owner_verbs.swap``.

Registered as the ``repl`` handler of the contributed row in
``core.money_verbs``. Same body as Telegram: same parse, quote-then-confirm,
sentences and policy chain.
"""
from __future__ import annotations


async def h_swap(ctx) -> None:
    """Quote a swap (a card); ``go`` swaps it."""
    from cli.ui.commands.h_owner import _tenant
    from tools.defi.owner_verbs.swap import swap_reply
    ctx.emit(await swap_reply(_tenant(ctx), list(ctx.args or []), seat="repl"), title="swap")
