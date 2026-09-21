"""h_gates.py — ``/gates`` (approval-gate management) and the ``/approve``
DECIDER (C5 / D78 / E16-adjacent, 2026-09-21).

⚠️ ``/approve`` meant two different things on two seats. On Telegram and in the
console it DECIDES one item of the owner queue; in this REPL it managed the
approval-GATE set (``list|add|remove``). So the REPL's own Inbox printed
"approve it with /approve <id>" and the seat answered ``unknown /approve
subcommand: '<id>'`` — the remedy named a command the seat could not run.

One name, one meaning:

* ``/approve <id>``        decides that pending item — the SAME decider
  ``/pending approve`` uses (``tools.controller.approval_queue.decide_pending``
  over ``all_pending``), so no two seats can disagree about what a decision is.
* ``/approve``             lists the pending union (nothing to guess at).
* ``/approve all``         decides the whole union (``decide_all_pending``).
* ``/gates list|add|remove`` is the gate manager, under its own name.
* ``/approve list|add|remove`` still works and prints the new name — a
  DEPRECATED ALIAS, because an owner with the old grammar in muscle memory must
  not be answered with "unknown subcommand".

The gate logic itself is unchanged and still lives in ``h_approve.py``
(``cmd_approve``); this module only routes.
"""
from __future__ import annotations

from typing import Any, Callable, List

from cli.ui import candy

#: The three gate subcommands, kept reachable under ``/approve`` as a
#: deprecated alias so no existing habit breaks silently.
_GATE_SUBCOMMANDS = ("list", "add", "remove")

_DEPRECATION = ("note: gate management moved to `/gates` — "
                "`/approve` now approves what is waiting on you.")


def _owner_home(ctx) -> str:
    """The ONE owner/admin home seam (031 deployed-home rule)."""
    from cli.ui.commands.h_owner import _admin_data_dir
    return _admin_data_dir(write=None)


def _decide(ctx, target: str) -> str:
    """Decide ONE pending item by id, over the ONE union."""
    import core.instance as _ci
    from tools.controller.approval_queue import all_pending, decide_pending

    uid = (getattr(ctx, "user_id", "") or "").strip() or "local"
    home_dir = _owner_home(ctx)
    instance_id = _ci.resolve_instance_id()

    pending_set = all_pending(user_id=uid, home_dir=home_dir,
                              instance_id=instance_id)
    match = next((it for it in pending_set.items if str(it["id"]) == target), None)
    if match is None:
        degraded = pending_set.degraded_line()
        tail = f"\n{degraded}" if degraded else ""
        return f"No pending item '{target}' — see /pending.{tail}"
    ok, msg = decide_pending(match["kind"], match["id"], approve=True,
                             user_id=uid, home_dir=home_dir,
                             instance_id=instance_id)
    return msg if ok else f"Failed: {msg}"


def _decide_all(ctx) -> str:
    import core.instance as _ci
    from tools.controller.approval_queue import decide_all_pending

    uid = (getattr(ctx, "user_id", "") or "").strip() or "local"
    ok_n, fail_n, msgs = decide_all_pending(
        approve=True, user_id=uid, home_dir=_owner_home(ctx),
        instance_id=_ci.resolve_instance_id())
    if not msgs:
        return candy.empty("pending items", "nothing is waiting on you", yet=False)
    return "\n".join(msgs + [f"{ok_n} approved, {fail_n} failed"])


def _list_pending(ctx) -> str:
    """What is waiting, with the exact command that decides each one."""
    import core.instance as _ci
    from tools.controller.approval_queue import all_pending

    uid = (getattr(ctx, "user_id", "") or "").strip() or "local"
    pending_set = all_pending(user_id=uid, home_dir=_owner_home(ctx),
                              instance_id=_ci.resolve_instance_id())
    items = pending_set.items
    if not items:
        return (pending_set.degraded_line()
                or candy.empty("items waiting on you", yet=False))
    lines = [f"{len(items)} waiting on you:"]
    for it in items:
        lines.append(candy.status_line(
            "pending", f"{it['id']}  [{it['kind']}]  {it.get('preview', '')}"))
    if pending_set.unavailable:
        lines.append(pending_set.degraded_line())
    lines.append("")
    lines.append(f"{candy.GUTTER}approve one: /approve <id>    all: /approve all"
                 f"    reject: /reject <id>")
    lines.append(f"{candy.GUTTER}manage approval GATES: /gates")
    return "\n".join(lines)


def h_approve(ctx) -> None:
    """``/approve [<id>|all|list|add|remove …]`` — decide, or the gate alias."""
    args = list(getattr(ctx, "args", None) or [])
    if not args:
        ctx.emit(_list_pending(ctx), title="approve")
        return
    first = args[0].lower()
    if first in _GATE_SUBCOMMANDS:
        # Deprecated alias — run it, and say where it lives now.
        out = _gates_body(ctx, args)
        ctx.emit(f"{out}\n\n{_DEPRECATION}", title="gates")
        return
    if first == "all":
        ctx.emit(_decide_all(ctx), title="approve")
        return
    try:
        ctx.emit(_decide(ctx, args[0]), title="approve")
    except Exception as exc:  # fail-open: never tear down the REPL dispatcher
        ctx.emit(f"approve failed: {exc}", title="approve")


def _gates_body(ctx, args: List[str]) -> str:
    from cli.ui.commands.h_approve import ApproveCtx, cmd_approve
    home_dir = _gates_home(ctx)
    gate_ctx = ApproveCtx(user_id=(getattr(ctx, "user_id", "") or "local"),
                          home_dir=home_dir)
    return cmd_approve(gate_ctx, list(args))


#: Set by :func:`register` — the prefs-home resolver ``handlers.py`` owns.
_HOME_RESOLVER: Any = None


def _gates_home(ctx) -> Any:
    if _HOME_RESOLVER is not None:
        return _HOME_RESOLVER(ctx)
    from core.runtime_paths import prefs_home_dir
    return prefs_home_dir()


def h_gates(ctx) -> None:
    """``/gates list|add <action>|remove <action>`` — the approval-gate set."""
    ctx.emit(_gates_body(ctx, list(getattr(ctx, "args", None) or [])), title="gates")


HELP_APPROVE = (
    "  Approve something that is waiting on you — a self-evolution proposal, a\n"
    "  queued spend/tool approval, or a pending correspondent. One name, one\n"
    "  meaning on every seat.\n"
    "\n"
    "    /approve              list what is waiting\n"
    "    /approve <id>         approve that one\n"
    "    /approve all          approve everything waiting\n"
    "\n"
    "  Approval GATES (which actions need a tap at all) moved to /gates.",
    "`/approve` on Telegram, and the Approve action in the console's Inbox.",
)

HELP_GATES = (
    "  Which actions need your approval before they run. This is policy, not a\n"
    "  queue: adding a gate TIGHTENS things and applies at once; removing one\n"
    "  loosens them, so it is queued for your own review first.\n"
    "\n"
    "    /gates                list every gated action and its source\n"
    "    /gates add <action>   require approval for that action\n"
    "    /gates remove <action>   queue its removal for review\n"
    "\n"
    "  An operator-controlled gate (env or compute posture) is explained, not\n"
    "  removed.",
    "`polyrob approvals` in a shell.",
)


def register(reg, Command, home_resolver: Callable[[Any], Any] = None) -> None:
    """Register ``/approve`` (decider) + ``/gates`` (gate manager).

    *home_resolver* is ``handlers._resolve_prefs_home_dir`` — injected rather
    than imported so this module never imports ``handlers`` (which imports it).
    """
    global _HOME_RESOLVER
    if home_resolver is not None:
        _HOME_RESOLVER = home_resolver
    reg.register(Command(
        "approve", h_approve,
        "Approve something that is waiting on you",
        usage="[<id>|all]", group="needs you",
        help_long=HELP_APPROVE[0], elsewhere=HELP_APPROVE[1],
    ))
    reg.register(Command(
        "gates", h_gates,
        "Which actions need your approval (list|add|remove)",
        usage="list | add <action> | remove <action>", group="needs you",
        help_long=HELP_GATES[0], elsewhere=HELP_GATES[1],
    ))
