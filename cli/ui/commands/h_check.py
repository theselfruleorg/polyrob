"""``/check`` in the REPL — the twin of ``surfaces/telegram/check_ops.py``.

Registered as the ``repl`` handler of the contributed row in
``core.money_verbs``. Same parse, same reads, same sentences.
"""
from __future__ import annotations


async def h_check(ctx) -> None:
    """A wallet's holdings or a token's report; a ticker's candidates."""
    from cli.ui.commands.h_owner import _tenant
    from surfaces.telegram.check_ops import check_reply
    ctx.emit(await check_reply(_tenant(ctx), list(ctx.args or [])), title="check")
