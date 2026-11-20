"""h_reject.py — ``/reject`` (top-level) in the REPL (043 A23).

The decision verb the REPL was missing: reject one pending item by id, the same
way ``/reject`` works on the phone. Until now the REPL could only reject through
the sub-arg ``/pending reject <kind> <id>``; a bare ``/reject <id>`` is the
parity gap this closes. (``/approve`` is its counterpart; the gate manager is
``/gates``.)

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
  with the kind resolved FROM the union by the one seat resolver
  (``approval_target.resolve_decision_target``), never guessed;
- no arg → list them (with any open money call card first); never decide.

⚠️ REACH, never policy: rejecting is the owner's own decision on his own queue —
this verb grants no authority the pending queue did not already give him.
"""
from __future__ import annotations

from cli.ui.commands.h_owner import _admin_data_dir, _tenant


def _board(data_dir: str):
    from modules.goals.board import GoalBoard
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
    from cli._admin_home import admin_instance
    instance_id = admin_instance()
    args = list(ctx.args or [])

    def _union():
        return all_pending(user_id=uid, home_dir=data_dir, instance_id=instance_id)

    # No arg: list, never decide (CLI4 — the same rule as /approve on every seat:
    # only an id or a tap decides). Open money call cards come first.
    if not args:
        from core.surfaces.card_list import call_cards_lines
        calls = "\n".join(call_cards_lines(uid))
        pending_set = _union()
        items = pending_set.items
        if not items:
            # C47: the ONE empty grammar (candy.empty), the same sentence
            # /approve renders over the same union.
            from cli.ui import candy
            ctx.emit(calls or pending_set.degraded_line()
                     or candy.empty("items waiting on you", yet=False),
                     title="reject")
            return
        lines = ([calls] if calls else []) + [f"{len(items)} pending — say which one:"]
        for it in items:
            lines.append(f"  /reject {it['id']}   [{it['kind']}]")
        if pending_set.unavailable:
            lines.append(pending_set.degraded_line())
        lines.append("All of them: /reject all")
        ctx.emit("\n".join(lines), title="reject")
        return
    target = args[0]

    # 126: a call card's id — /reject is the typed form of its Cancel.
    from cli.ui.commands.h_gates import _call_card_press
    tapped = _call_card_press(ctx, f"/reject {target}")
    if tapped is not None:
        return tapped

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
    # proposal 125 S10: the ONE resolver every seat uses — alias, id, `kind:id`,
    # `<id>@<rev>`, a named ambiguity, and the gone-tap fallback.
    from tools.controller.approval_target import resolve_decision_target
    hit = resolve_decision_target(target, pending_set)
    if hit.refusal:
        ctx.emit(hit.refusal, title="reject")
        return
    match = hit.item
    if match is None:
        degraded = pending_set.degraded_line()
        tail = f"\n{degraded}" if degraded else ""
        ctx.emit(f"No pending item '{target}' — see /pending.{tail}", title="reject")
        return
    ok, msg = decide_pending(match["kind"], match["id"], approve=False,
                             rev=match.get("seat_rev"),
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
    "    /reject               list what waits (it never decides)\n"
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
