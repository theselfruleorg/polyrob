"""h_workers.py — ``/workers`` in the REPL (041 phase 2).

REPL-only (``core.verbs`` row ``seats=("repl",)``): the terminal is the seat
that holds the live session, so it is where the running workers' Stop / Steer
belong. The store half renders through the SAME helpers as ``polyrob workers``
(``cli/commands/workers.py``), against the same deployed home.

    /workers                       approved + pending, and what is running now
    /workers show <id>             one worker
    /workers approve <id>          approve a worker the agent proposed
    /workers remove <id>           archive one
    /workers stop <run-id>         stop a running worker at its next step
    /workers steer <run-id> <text> one guidance line into a running worker

Stop and Steer act on THIS session's ``SubAgentManager`` (the live child), the
same objects ``delegate_task`` spawns — no second registry.
"""
from __future__ import annotations


def _manager(ctx):
    orch = getattr(ctx, "orchestrator", None)
    return getattr(orch, "sub_agent_manager", None) if orch is not None else None


def _store(*, write: bool):
    from cli._admin_home import admin_data_dir
    from agents.task.agent.profile_store import ProfileStore
    return ProfileStore(home_dir=admin_data_dir(write=write))


async def h_workers(ctx) -> None:
    from cli.commands import workers as W

    args = list(ctx.args or [])
    sub = args[0].lower() if args else ""
    from cli._admin_home import admin_owner_tenant
    uid = admin_owner_tenant(ctx.user_id)
    try:
        if sub in ("", "list"):
            body = W.render_list(_store(write=False), uid)
            mgr = _manager(ctx)
            if mgr is not None:
                body += "\n\n" + W.render_live(mgr)
            ctx.emit(body, title="workers")
            return
        if sub == "show" and len(args) > 1:
            ctx.emit(W.render_show(_store(write=False), uid, args[1]), title="workers")
            return
        if sub == "approve" and len(args) > 1:
            ctx.emit(W.approve_worker(_store(write=True), uid, args[1]), title="workers")
            return
        if sub == "remove" and len(args) > 1:
            ctx.emit(W.remove_worker(_store(write=True), uid, args[1]), title="workers")
            return
        if sub == "stop" and len(args) > 1:
            mgr = _manager(ctx)
            cid = mgr.stop_child(args[1]) if mgr is not None else None
            ctx.emit(f"Stopping {cid} — it ends at its next step." if cid
                     else f"No running worker '{args[1]}' in this session.", title="workers")
            return
        if sub == "steer" and len(args) > 2:
            mgr = _manager(ctx)
            cid = (await mgr.steer_child(args[1], " ".join(args[2:]))
                   if mgr is not None else None)
            ctx.emit(f"Steered {cid} — it reads this at its next step." if cid
                     else f"No running worker '{args[1]}' in this session.", title="workers")
            return
    except Exception as exc:  # an owner seat reports, it never crashes the REPL
        ctx.emit(f"Workers: {exc}", title="workers")
        return
    ctx.emit("Usage: /workers [show|approve|remove <id> | stop <run-id> | "
             "steer <run-id> <text>]", title="workers")


HELP_WORKERS = (
    "  Named workers are reusable helpers the agent dispatches by name. You see\n"
    "  the approved ones, approve what the agent proposed, and Stop or Steer a\n"
    "  worker that is running in this session.\n"
    "\n"
    "    /workers approve researcher\n"
    "    /workers steer sub_ab12 cite primary sources only",
    "`polyrob workers` in the terminal (list/show/new/edit/approve/remove).",
)


def register(reg, Command) -> None:
    """Register ``/workers``. Called from ``h_a23.register``."""
    reg.register(Command(
        "workers", h_workers,
        "Named workers: approve, and stop or steer a running one",
        usage="[show|approve|remove|stop|steer] …", group="set up", raw_arguments=True,
        help_long=HELP_WORKERS[0], elsewhere=HELP_WORKERS[1],
    ))
