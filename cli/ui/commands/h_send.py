"""``/send`` in the REPL — the seat half of ``tools.defi.owner_verbs.send``.

Registered as the ``repl`` handler of the contributed row in
``core.money_verbs``. Same body as Telegram: same parse, quote-then-``go``,
sentences and policy chain.
"""
from __future__ import annotations


async def h_send(ctx) -> None:
    """Quote a send; ``go`` sends it."""
    from cli.ui.commands.h_owner import _tenant
    from tools.defi.owner_verbs.send import send_reply
    ctx.emit(await send_reply(_tenant(ctx), list(ctx.args or []), seat="repl"), title="send")
