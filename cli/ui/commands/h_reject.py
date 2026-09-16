"""h_reject.py — ``/reject`` (top-level) in the REPL (043 A23).

The decision verb the REPL was missing: reject one pending item by id, the same
way ``/reject`` works on the phone. Until now the REPL could only reject through
the sub-arg ``/pending reject <kind> <id>``; a bare ``/reject <id>`` is the
parity gap this closes. (``/approve`` in the REPL is a DIFFERENT verb — the gate
manager — so ``/reject`` deliberately does NOT alias it.)

Thin over the SAME primitives every seat uses, mirroring
``surfaces.telegram.harness``'s ``/reject`` branch so a rejection means the same
thing everywhere:

- ``all``                     → ``self_evolution.decide_all(approve=False)``
- a ``tap-<id>`` approval ask → ``approval_queue.decide_tool_approval(approved=False)``
- a ``<surface>:<address>``   → the correspondent registry's real reject
- otherwise                   → ``self_evolution.reject(kind, id)`` by matched id
- no arg + exactly one pending → that one; more than one → list them

⚠️ REACH, never policy: rejecting is the owner's own decision on his own queue —
this verb grants no authority the pending queue did not already give him.
"""
from __future__ import annotations

from cli.ui.commands.h_owner import _admin_data_dir, _tenant


def _board(data_dir: str):
    from agents.task.goals.board import GoalBoard
    from core.runtime_paths import goals_db_path
    return GoalBoard(goals_db_path(data_dir))


def h_reject(ctx) -> None:
    """Reject one pending item (proposal / approval ask / correspondent)."""
    import core.instance as _ci
    from core import self_evolution

    uid = _tenant(ctx)
    # The REPL is a trusted local operator surface ({cli,local,repl}); the local
    # bypass is the documented owner check for it (same gate as /pending).
    if not _ci.is_owner(uid, local=True):
        ctx.emit("(owner-only command — the review queue gates self-evolution)",
                 title="reject")
        return

    data_dir = _admin_data_dir(ctx)
    instance_id = _ci.resolve_instance_id()
    args = list(ctx.args or [])

    # No arg: decide the queue only when it is unambiguous, else list it — never
    # guess which item the owner meant.
    if not args:
        items = self_evolution.list_pending(uid, home_dir=data_dir, instance_id=instance_id)
        if not items:
            ctx.emit("Nothing pending — there is nothing waiting on you.", title="reject")
            return
        if len(items) > 1:
            lines = [f"{len(items)} pending — say which one:"]
            for it in items:
                idv = str(it["id"])
                lines.append(f"  /reject {it['kind']}:{idv}" if ":" not in idv
                             else f"  /reject {idv}")
            lines.append("All of them: /reject all")
            ctx.emit("\n".join(lines), title="reject")
            return
        target = str(items[0]["id"])
    else:
        target = args[0]

    # `/reject all` — clear the whole self-evolution queue.
    if target.lower() == "all":
        ok_n, fail_n, msgs = self_evolution.decide_all(
            False, user_id=uid, home_dir=data_dir, instance_id=instance_id)
        if not msgs:
            ctx.emit("No pending proposals.", title="reject")
            return
        ctx.emit("\n".join(msgs + [f"{ok_n} rejected, {fail_n} failed"]), title="reject")
        return

    # A tool-approval ask is namespaced `tap-<id>` so a bare id never collides
    # with a self-evolution proposal id.
    from tools.controller.approval_queue import decide_tool_approval, strip_tap_prefix
    if strip_tap_prefix(target) is not None:
        ok, msg = decide_tool_approval(_board(data_dir), target, user_id=uid,
                                       approved=False)
        ctx.emit(msg if ok else f"Failed: {msg}", title="reject")
        return

    # A correspondent item's id is `<surface>:<address>`. Reject = a real
    # tombstone that blocks a silent re-seed (matches the console + Telegram).
    if ":" in target:
        from surfaces.telegram.harness import _correspondent_decision
        reply = _correspondent_decision(data_dir, target, uid, approve=False)
        if reply is not None:
            ctx.emit(reply, title="reject")
            return

    # Otherwise it is a self-evolution proposal id — reject (archive, recoverable).
    items = self_evolution.list_pending(uid, home_dir=data_dir, instance_id=instance_id)
    match = next((it for it in items if str(it["id"]) == target), None)
    if match is None:
        ctx.emit(f"No pending proposal '{target}' — see /pending.", title="reject")
        return
    ok, msg = self_evolution.reject(match["kind"], match["id"], user_id=uid,
                                    home_dir=data_dir, instance_id=instance_id)
    ctx.emit(msg if ok else f"Failed: {msg}", title="reject")


HELP_REJECT = (
    "  Reject ONE thing waiting on you, by id — a self-evolution proposal, a\n"
    "  spend/tool approval ask (tap-<id>), or a pending correspondent\n"
    "  (<surface>:<address>). The counterpart to approving it.\n"
    "\n"
    "    /reject <id>          reject that item\n"
    "    /reject all           reject every pending proposal\n"
    "    /reject               with one item waiting, reject it; else list them\n"
    "\n"
    "  Rejecting a correspondent tombstones it so it cannot silently re-seed.\n"
    "  (In this REPL /approve is the gate manager, so /reject does not pair\n"
    "  with it — use /pending approve or /inbox to accept.)",
    "`/reject` on Telegram, and the Reject action in the console's Inbox.",
)


def register(reg, Command) -> None:
    """Register ``/reject``. Called from ``h_a23.register``."""
    reg.register(Command(
        "reject", h_reject,
        "Reject one pending item by id (proposal / approval ask / correspondent)",
        usage="[<id>|all]", group="needs you",
        help_long=HELP_REJECT[0], elsewhere=HELP_REJECT[1],
    ))
