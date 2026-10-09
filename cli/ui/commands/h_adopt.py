"""``/adopt`` in the REPL — the twin of ``surfaces/telegram/adopt_ops.py``.

Registered as the ``repl`` handler of the contributed row in
``core.standing_verbs``. Same parse, same quote, same confirm line (the REPL's
``wrap_quote`` makes the card). The bare REPL refuses in a process the agent
started (``cli/agent_child_gate.py``), so the agent cannot type it here.
"""
from __future__ import annotations


async def h_adopt(ctx) -> None:
    """Make an agent-authored cron job or goal owner-authored."""
    from cli.ui.commands.h_owner import _admin_data_dir, _tenant
    from surfaces.telegram.adopt_ops import adopt_reply
    ctx.emit(adopt_reply(_tenant(ctx), _admin_data_dir(write=None), list(ctx.args or [])),
             title="adopt")
