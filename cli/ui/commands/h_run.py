"""``/run`` in the REPL — the twin of Telegram ``/run``.

One background run: list, pause, resume, stop. Same rows, same mailbox, same
sentences — both seats render :func:`core.run_control.run_reply`.
"""
from __future__ import annotations


async def h_run(ctx) -> None:
    """`/run [pause|resume|stop <n>]` — one background run."""
    from agents.task.path import pm
    from cli.ui.commands.h_owner import _admin_data_dir, _tenant
    from core.run_control import run_reply
    ctx.emit(await run_reply(_tenant(ctx), _admin_data_dir(write=False), str(pm().data_root),
                             list(getattr(ctx, "args", []) or []),
                             exclude=(getattr(ctx, "session_id", None),)),
             title="run")
