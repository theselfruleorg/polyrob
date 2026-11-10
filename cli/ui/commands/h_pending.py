"""h_pending.py — ``/pending`` in the REPL: the owner review queue.

Extracted from ``handlers.py`` (2026-09-15): that file is at its size ratchet
(``tests/test_file_size_ratchet.py`` — extract new behaviour into a new module,
never grow the god-file), the same call ``h_inbox.py`` and ``h_help.py`` already
made. ``handlers.py`` re-exports the handler, so registration is unchanged.

What is NOT here: the queue itself and the deciders. Both live in
``tools.controller.approval_queue`` (``all_pending`` / ``decide_pending`` /
``decide_all_pending``), because Telegram, the CLI and this seat must list and
decide over the SAME set — they did not, and two of the three read only the
first of three sources.
"""
from cli.ui.commands.registry import CommandContext


def _h_pending(ctx: CommandContext) -> None:
    """Owner review queue for the agent's self-evolution proposals (T4-06b/T4-07).

    Umbrella over `core.self_evolution` — the same pipeline `polyrob owner
    pending/promote/reject` administers, now reachable without leaving the REPL.
    (NOT the marketplace-install quarantine — that stays under `/skills approve`.)

    Usage:
      /pending                       — list pending proposals (skills + identity notes)
      /pending show <kind> <id>      — the whole item: why, what changes, the diff
                                        (any kind /pending lists)
      /pending approve <kind> <id>   — promote to active
                                        (kind: skill | self_context | owner_doc |
                                         contract | pref_change)
      /pending reject <kind> <id>    — reject (archive, recoverable)
    """
    import core.instance as _ci
    from cli.ui import candy
    from core import self_evolution

    from cli._admin_home import admin_owner_tenant
    uid = admin_owner_tenant(ctx.user_id)
    # The REPL is a trusted local operator surface ({cli,local,repl}); the
    # local=True bypass is the documented owner check for it. A bound owner
    # principal always wins; an unbound local operator IS the owner here.
    if not _ci.is_owner(uid, local=True):
        ctx.emit("(owner-only command — the review queue gates self-evolution)")
        return

    # C2: the ONE owner/admin home seam (031 deployed-home rule) — the same
    # home `polyrob owner pending` reads. Deciding is a WRITE.
    from cli.ui.commands.h_owner import _admin_data_dir
    home_dir = _admin_data_dir(write=None)
    from cli._admin_home import admin_instance
    instance_id = admin_instance()

    args = list(ctx.args or [])
    # A tapped `/pending_p_<hex>` (or `/pending p-<hex>`): ONE item's full
    # review — a skill update as a diff against the live skill.
    if len(args) == 1 and args[0].lower().startswith(self_evolution.PENDING_ALIAS_PREFIX):
        from tools.controller.approval_queue import all_pending, resolve_pending_target
        match = resolve_pending_target(args[0], all_pending(
            user_id=uid, home_dir=home_dir, instance_id=instance_id))
        if match is None:
            ctx.emit(f"No pending item '{args[0]}' — see /pending.", title="pending")
            return
        _emit_review(ctx, match, uid=uid, home_dir=home_dir, instance_id=instance_id)
        return
    # 035 P1-10: `/pending approve all` / `/pending reject all`.
    if len(args) == 2 and args[0].lower() in ("approve", "promote", "reject") \
            and args[1].lower() == "all":
        # 2026-09-15: ONE decider over the ONE union. This branch used to read
        # the self-evolution third of the queue while the Inbox beside it showed
        # all three, so a queued spend approval survived "approve all" silently.
        from tools.controller.approval_queue import decide_all_pending
        ok_n, fail_n, msgs = decide_all_pending(
            approve=args[0].lower() != "reject", user_id=uid, home_dir=home_dir,
            instance_id=instance_id)
        if not msgs:
            ctx.emit(candy.empty("pending items", "nothing is waiting on you",
                                 yet=False), title="pending")
            return
        verb = "rejected" if args[0].lower() == "reject" else "promoted"
        ctx.emit("\n".join(msgs + [f"{ok_n} {verb}, {fail_n} failed"]), title="pending")
        return
    if args and args[0].lower() in ("approve", "promote", "reject", "show"):
        if len(args) < 3:
            ctx.emit("usage: /pending show|approve|reject <kind> <id>   "
                     "(kind: skill | self_context | owner_doc | contract | pref_change)"
                     "\n       /pending approve all   — decide everything at once")
            return
        verb, kind, item_id = args[0].lower(), args[1], " ".join(args[2:])
        if verb == "show":
            # 096 W7: the same review every seat opens — Why, Changes, the diff
            # (both halves of a linked pair), a tool approval's full card.
            from core.pending_seats import find_item
            from tools.controller.approval_queue import all_pending
            match = find_item(all_pending(user_id=uid, home_dir=home_dir,
                                          instance_id=instance_id).items, kind, item_id)
            if match is None:
                ctx.emit(f"No pending {kind} '{item_id}' — see /pending.", title="pending")
                return
            _emit_review(ctx, match, uid=uid, home_dir=home_dir, instance_id=instance_id)
            return
        # ONE decider, so this seat can execute the command its OWN Inbox
        # advertises: `core.surfaces.inbox_render.REPL_REMEDIES` prints
        # `/pending approve tool_approval <id>` and `/pending approve
        # correspondent <id>`, and both kinds used to reach the self-evolution
        # promoter, which answered "unknown pending kind".
        from tools.controller.approval_queue import decide_pending
        ok, msg = decide_pending(kind, item_id, approve=(verb != "reject"),
                                 user_id=uid, home_dir=home_dir,
                                 instance_id=instance_id)
        ctx.emit(msg, title="pending")
        return

    # The ONE union — proposals, queued tool/spend approvals AND pending
    # contacts. Listing a third of the queue and calling it "pending" is the
    # confident-zero this surface exists to refuse.
    from tools.controller.approval_queue import all_pending
    pending_set = all_pending(user_id=uid, home_dir=home_dir,
                              instance_id=instance_id)
    items = pending_set.items
    if not items:
        ctx.emit(pending_set.degraded_line() or "No pending approvals.",   # the chat seat's words
                 title="pending")
        return
    # 096 W7 (R4): the rows the Telegram notice shows — what · Why · Changes,
    # a linked pair folded to one decision — never the raw preview.
    from core.pending_seats import SEAT_REPL, fold_linked, list_lines
    lines = [f"{len(fold_linked(items))} pending proposal(s):"]
    lines.extend(f"{candy.GUTTER}{line}" for line in list_lines(items, seat=SEAT_REPL))
    lines.append("")
    if pending_set.unavailable:
        lines.append(pending_set.degraded_line())
    lines.append("all at once: /pending approve all    /pending reject all")
    ctx.emit("\n".join(lines), title="pending")


def _emit_review(ctx: CommandContext, item: dict, *, uid: str, home_dir, instance_id: str) -> None:
    """ONE pending item in full, through the shared seat renderer."""
    from core.pending_seats import SEAT_REPL, review_one
    ok, body = review_one(item, user_id=uid, home_dir=home_dir,
                          instance_id=instance_id, seat=SEAT_REPL)
    ctx.emit(body, title=f"pending {item['kind']}:{item['id']}" if ok else "pending")
