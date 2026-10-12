"""``/restart`` and ``/update`` in the REPL — the twins of
``surfaces/telegram/lifecycle_ops.py`` (077).

Same parse, same quote, same confirm line (the REPL's ``wrap_quote`` makes the
card). They act on the BACKGROUND SERVICE (the process that runs the chat
surfaces), never on this REPL: a REPL keeps the code it started with until you
leave it and start it again.
"""
from __future__ import annotations


def _origin(ctx) -> dict:
    from cli.ui.commands.h_owner import _tenant
    return {"surface": "repl", "user_id": _tenant(ctx)}


async def h_restart(ctx) -> None:
    """Restart the background service once its current work is done."""
    from cli.ui.commands.h_owner import _admin_data_dir, _tenant
    from surfaces.telegram.lifecycle_ops import restart_reply
    ctx.emit(restart_reply(_tenant(ctx), _admin_data_dir(write=None),
                           list(ctx.args or []), _origin(ctx)), title="restart")


async def h_update(ctx) -> None:
    """Update the background service to the latest release."""
    from cli.ui.commands.h_owner import _admin_data_dir, _tenant
    from surfaces.telegram.lifecycle_ops import update_reply
    ctx.emit(await update_reply(_tenant(ctx), _admin_data_dir(write=None),
                                list(ctx.args or []), _origin(ctx)), title="update")
