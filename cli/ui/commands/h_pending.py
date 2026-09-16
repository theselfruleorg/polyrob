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
from core.runtime_paths import data_dir_or_home


def _h_pending(ctx: CommandContext) -> None:
    """Owner review queue for the agent's self-evolution proposals (T4-06b/T4-07).

    Umbrella over `core.self_evolution` — the same pipeline `polyrob owner
    pending/promote/reject` administers, now reachable without leaving the REPL.
    (NOT the marketplace-install quarantine — that stays under `/skills approve`.)

    Usage:
      /pending                       — list pending proposals (skills + identity notes)
      /pending show <kind> <id>      — full-body review of one proposal (T3-09)
      /pending approve <kind> <id>   — promote to active
                                        (kind: skill | self_context | owner_doc |
                                         contract | pref_change)
      /pending reject <kind> <id>    — reject (archive, recoverable)
    """
    import core.instance as _ci
    from cli.ui import candy
    from core import self_evolution

    uid = (ctx.user_id or "").strip() or "local"
    # The REPL is a trusted local operator surface ({cli,local,repl}); the
    # local=True bypass is the documented owner check for it. A bound owner
    # principal always wins; an unbound local operator IS the owner here.
    if not _ci.is_owner(uid, local=True):
        ctx.emit("(owner-only command — the review queue gates self-evolution)")
        return

    cfg = getattr(ctx.container, "config", None) if ctx.container else None
    home_dir = data_dir_or_home(getattr(cfg, "data_dir", None))
    instance_id = _ci.resolve_instance_id()

    args = list(ctx.args or [])
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
            ctx.emit("no pending proposals", title="pending")
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
            ok, body = self_evolution.show(kind, item_id, user_id=uid,
                                           home_dir=home_dir, instance_id=instance_id)
            ctx.emit(body, title=f"pending {kind}:{item_id}" if ok else "pending")
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
        ctx.emit(pending_set.degraded_line()
                 or candy.empty("pending proposals",
                                "/pending approve <kind> <id> reviews one"),
                 title="pending")
        return
    lines = [f"{len(items)} pending proposal(s):"]
    for it in items:
        # pending_kind_label is landing from a parallel wave; fall back to the
        # raw kind so /pending never breaks on a tree without it.
        label = getattr(self_evolution, "pending_kind_label", lambda k: k)(it["kind"])
        lines.append(candy.status_line(
            "pending", f"[{label}] {it['kind']}:{it['id']}  ({it['chars']} chars)"
        ))
        lines.append(f"{candy.GUTTER}  {it['preview']}")
        # 035 P0-5: name the ACTIVE rule this draft contradicts.
        for c in (it.get("conflicts") or []):
            lines.append(f"{candy.GUTTER}  ⚠ CONFLICT — {c}")
    lines.append("")
    if pending_set.unavailable:
        lines.append(pending_set.degraded_line())
    lines.append("approve: /pending approve <kind> <id>    reject: /pending reject <kind> <id>"
                 "    all: /pending approve all")
    ctx.emit("\n".join(lines), title="pending")
