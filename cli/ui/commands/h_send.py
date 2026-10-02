"""``/send`` in the REPL — the twin of ``surfaces/telegram/send_ops.py``.

Registered as the ``repl`` handler of the contributed row in
``core.money_verbs``. Same parse, same quote-then-``go``, same sentences.
"""
from __future__ import annotations


async def h_send(ctx) -> None:
    """Quote a send; ``go`` sends it."""
    from cli.ui.commands.h_owner import _tenant
    from surfaces.telegram.send_ops import send_reply
    ctx.emit(await send_reply(_tenant(ctx), list(ctx.args or [])), title="send")
