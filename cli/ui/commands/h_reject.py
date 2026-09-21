"""h_reject.py — ``/reject`` (top-level) in the REPL (043 A23).

The decision verb the REPL was missing: reject one pending item by id, the same
way ``/reject`` works on the phone. Until now the REPL could only reject through
the sub-arg ``/pending reject <kind> <id>``; a bare ``/reject <id>`` is the
parity gap this closes. (``/approve`` in the REPL is a DIFFERENT verb — the gate
manager — so ``/reject`` deliberately does NOT alias it.)

Thin over the SAME primitives every seat uses, mirroring
``surfaces.telegram.harness``'s ``/reject`` branch so a rejection means the same
thing everywhere. ONE queue, ONE decider (C4, 2026-09-21):

- listing and matching read ``approval_queue.all_pending`` — the UNION of
  self-evolution proposals, queued tool/spend approvals and pending
  correspondents. This handler used to read only the self-evolution third while
  ``/pending`` beside it showed all three, so a queued spend approval could not
  be rejected here at all and ``/reject`` with one payment waiting answered
  "Nothing pending";
- ``all``          → ``approval_queue.decide_all_pending(approve=False)``;
- ``<id>``         → ``approval_queue.decide_pending(kind, id, approve=False)``
  with the kind resolved FROM the union, never guessed;
- no arg + exactly one pending → that one; more than one → list them.

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
    from tools.controller.approval_queue import (
        all_pending, decide_all_pending, decide_pending,
    )

    uid = _tenant(ctx)
    # The REPL is a trusted local operator surface ({cli,local,repl}); the local
    # bypass is the documented owner check for it (same gate as /pending).
    if not _ci.is_owner(uid, local=True):
        ctx.emit("(owner-only command — the review queue gates self-evolution)",
                 title="reject")
        return

    data_dir = _admin_data_dir(write=None)
    instance_id = _ci.resolve_instance_id()
    args = list(ctx.args or [])

    def _union():
        return all_pending(user_id=uid, home_dir=data_dir, instance_id=instance_id)

    # No arg: decide the queue only when it is unambiguous, else list it — never
    # guess which item the owner meant.
    if not args:
        pending_set = _union()
        items = pending_set.items
        if not items:
            # C47: the ONE empty grammar (candy.empty), the same sentence
            # /approve renders over the same union.
            from cli.ui import candy
            ctx.emit(pending_set.degraded_line()
                     or candy.empty("items waiting on you", yet=False),
                     title="reject")
            return
        if len(items) > 1:
            lines = [f"{len(items)} pending — say which one:"]
            for it in items:
                lines.append(f"  /reject {it['id']}   [{it['kind']}]")
            if pending_set.unavailable:
                lines.append(pending_set.degraded_line())
            lines.append("All of them: /reject all")
            ctx.emit("\n".join(lines), title="reject")
            return
        target = str(items[0]["id"])
    else:
        target = args[0]

    # `/reject all` — the WHOLE union, not the self-evolution third of it.
    if target.lower() == "all":
        ok_n, fail_n, msgs = decide_all_pending(
            approve=False, user_id=uid, home_dir=data_dir, instance_id=instance_id)
        if not msgs:
            from cli.ui import candy
            ctx.emit(candy.empty("items waiting on you", yet=False), title="reject")
            return
        ctx.emit("\n".join(msgs + [f"{ok_n} rejected, {fail_n} failed"]), title="reject")
        return

    pending_set = _union()
    match = next((it for it in pending_set.items if str(it["id"]) == target), None)
    if match is None:
        degraded = pending_set.degraded_line()
        tail = f"\n{degraded}" if degraded else ""
        ctx.emit(f"No pending item '{target}' — see /pending.{tail}", title="reject")
        return
    ok, msg = decide_pending(match["kind"], match["id"], approve=False,
                             user_id=uid, home_dir=data_dir,
                             instance_id=instance_id)
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
    "  The counterpart is /approve <id>; /gates is where approval GATES live.",
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
