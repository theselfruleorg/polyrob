"""``/check`` in the REPL — the seat half of ``tools.defi.owner_verbs.check``.

Registered as the ``repl`` handler of the contributed row in
``core.money_verbs``. The read is the agent's own ``defi_data.address_report``.
"""
from __future__ import annotations


async def h_check(ctx) -> None:
    """A wallet's holdings or a token's report; a ticker's candidates."""
    from cli.ui.commands.h_owner import _tenant
    from tools.defi.owner_verbs.check import check_reply
    ctx.emit(await check_reply(_tenant(ctx), list(ctx.args or []), seat="repl"), title="check")
