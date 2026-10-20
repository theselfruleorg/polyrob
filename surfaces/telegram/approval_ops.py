"""The owner's decision verbs on the Telegram seat: /pending, /approve,
/reject, /asks, /fulfill and the outbound allowlist (/allow, /deny,
/allowlist).

Moved out of ``surfaces/telegram/harness.py::_handle_owner_admin`` (W9,
2026-10-10 structural review) unchanged. Each ``*_verb`` takes the one
:class:`surfaces.telegram.admin_verbs.AdminCall` and runs AFTER the owner gate
(``admin_verbs`` owns the table). The three helpers keep their names because
the harness re-exports them (tests and the REPL parity checks read them there).
Imports nothing from the harness (no import cycle).
"""
from core.runtime_paths import correspondents_db_path, surfaces_db_path
import logging
import os
from typing import Any, Optional

from core.surfaces.tappable import parse_ask_option as _parse_ask_option

logger = logging.getLogger("surfaces.telegram.harness")


def _correspondent_decision(data_dir: str, target: str, user_id: str,
                            *, approve: bool) -> Optional[str]:
    """Approve a pending `<surface>:<address>` correspondent from chat.

    Returns None when TARGET is not a pending correspondent, so the caller falls
    through to the self-evolution proposal path (an id may legitimately contain
    a colon). 030 C3: the registry now has a real ``reject()`` (pending →
    expired tombstone, blocks a silent re-seed) — chat uses it, matching the
    webview Review page.
    """
    surface, _, address = target.partition(":")
    if not surface or not address:
        return None
    from core.surfaces.correspondents import CorrespondentRegistry
    registry = CorrespondentRegistry(correspondents_db_path(data_dir))
    try:
        pending = [r for r in registry.list(user_id=user_id)
                   if r.get("state") == "pending"
                   and f"{r['surface']}:{r['address']}" == target]
    except Exception as e:
        logger.warning("telegram correspondent decision: registry read failed: %s", e,
                       exc_info=True)
        return None
    if not pending:
        return None
    if not approve:
        try:
            ok = registry.reject(surface=surface, address=address, user_id=user_id)
        except Exception:
            ok = False
        if ok:
            return (f"🚫 Rejected {target} — the pending binding is tombstoned "
                    "and cannot silently re-seed.")
        return (f"'{target}' stays pending — a pending contact is already denied "
                "(their replies never reach me).")
    ok = registry.approve(surface=surface, address=address, user_id=user_id)
    if ok:
        return f"✅ Approved {target} — their replies now reach me as data."
    return (f"Failed to approve {target} — it is no longer pending. "
            f"See /pending for what is.")


def _pending_set(user_id: str, data_dir: str, instance_id: str, board: Any):
    """This seat's read of the ONE pending union (`approval_queue.all_pending`).

    Reuses the board the handler already built, so `/pending` and `/approve` do
    not open `goals.db` twice for one message.
    """
    from core.surfaces.correspondents import CorrespondentRegistry
    from tools.controller.approval_queue import all_pending
    try:
        registry = CorrespondentRegistry(correspondents_db_path(data_dir))
    except Exception:
        logger.warning("telegram: correspondent registry unavailable", exc_info=True)
        registry = None
    return all_pending(user_id=user_id, home_dir=data_dir, instance_id=instance_id,
                       board=board, correspondent_registry=registry)


async def _decide_pending_item(task_agent: Any, item: dict, *, approve: bool,
                               user_id: str, data_dir: str, instance_id: str,
                               board: Any) -> tuple:
    """Record the owner's decision on ONE item of the union. ``(ok, message)``.

    The union holds three kinds of thing and each has its own decider; routing
    lives here so the single-item path and `/approve_all` can never disagree
    about what a decision means.
    """
    from tools.controller.approval_queue import decide_pending
    kind = item.get("kind", "")
    if kind == "correspondent":
        # This seat keeps its own owner-facing sentence for a contact — it names
        # what changes about THEIR replies, which the generic decider does not.
        # A successful decision is marked with a leading glyph; read that rather
        # than the prose, so a reworded failure is never counted as a win.
        reply = _correspondent_decision(data_dir, str(item.get("id", "")),
                                        user_id, approve=approve)
        if reply is not None:
            return reply.startswith(("✅", "🚫")), reply
        return False, f"could not decide correspondent {item.get('id')}"
    return decide_pending(kind, item.get("id"), approve=approve, user_id=user_id,
                          home_dir=data_dir, instance_id=instance_id,
                          board=board, task_agent=task_agent)


async def pending_verb(c) -> str:
    """`/pending`. ONE union — the same one `/approve` decides over (2026-09-15).
    Chat used to build this join by hand while the bare `/approve` shortcut read
    only the first of the three sources; see `approval_queue.all_pending`."""
    from core import self_evolution
    pending = _pending_set(c.user_id, c.data_dir, c.instance_id, c.board)
    rest = _elsewhere(c, "pending")
    if not pending.items:
        return "\n".join(x for x in (pending.degraded_line() or "No pending approvals.",
                                       rest) if x)
    lines = [f"{len(pending.items)} pending approval(s):"]
    for it in pending.items:
        preview = (it.get("preview") or "").strip()
        if len(preview) > 160:
            preview = preview[:157] + "…"
        lines.append(f"• {it['kind']}:{it['id']} — {preview}")
        lines.append(f"   {self_evolution.pending_tap_token('approve', it)}   "
                     f"{self_evolution.pending_tap_token('reject', it)}")
    lines.append("Everything at once: /approve_all")
    if pending.degraded_line():
        lines.append(pending.degraded_line())
    if rest:
        lines.append(rest)
    return "\n".join(lines)


def _elsewhere(c, view_name: str) -> str:
    """080 WS-3: ``/pending`` and ``/asks`` are views of the one Inbox — say
    what else waits there. Fail-open to nothing (the view itself is honest)."""
    try:
        from core.surfaces.inbox_render import elsewhere_line
        from surfaces.inbox_sources import build_inbox
        return elsewhere_line(build_inbox(c.user_id, data_dir=c.data_dir,
                                          instance_id=c.instance_id), view_name)
    except Exception:
        logger.debug("inbox elsewhere line skipped", exc_info=True)
        return ""


async def decide_verb(c) -> str:
    """`/approve` and `/reject`."""
    from core import self_evolution
    cmd, args = c.cmd, c.args
    user_id, data_dir, instance_id, board = c.user_id, c.data_dir, c.instance_id, c.board
    approve = (cmd == "/approve")
    pending = _pending_set(user_id, data_dir, instance_id, board)
    if not args:
        # Owner complaint, 2026-09-12: "the whole command should be
        # highlighted so I could tap on it. now I need to copy and type in
        # the id". Telegram auto-links the VERB `/approve` but not its
        # argument, so every item is listed with its ONE-token tap.
        # ⚠️ CLI4 (audit 2026-10-03): the bare verb used to DECIDE when
        # exactly one item waited. On the console (which shares this
        # handler) that approved an item the owner never saw, and the REPL
        # lists. A bare verb now always shows; only an id or a tap decides.
        if not pending.items:
            return (pending.degraded_line()
                    or "Nothing pending — there is nothing waiting on you.")
        _n = len(pending.items)
        _lines = [f"{_n} pending — tap {'it' if _n == 1 else 'the one you mean'}"
                  f" to {cmd.lstrip('/')}:"]
        for _it in pending.items:
            _preview = " ".join(str(_it.get("preview") or "").split())
            if len(_preview) > 120:
                _preview = _preview[:117] + "…"
            _lines.append(
                f"  {self_evolution.pending_tap_token(cmd.lstrip('/'), _it)}"
                f" — {_it['kind']}:{_it['id']}"
                + (f" — {_preview}" if _preview else ""))
        if _n > 1:
            _lines.append(f"All of them: {cmd}_all")
        if pending.degraded_line():
            _lines.append(pending.degraded_line())
        return "\n".join(_lines)
    target = args[0]
    # 035 P1-10: `/approve all` — decide the whole queue from the phone. The
    # owner surface where friction costs most is the one where typing an id
    # is hardest. 2026-09-15: "the whole queue" now means the UNION it is
    # advertised as, not the self-evolution third of it.
    if target.lower() == "all":
        if not pending.items:
            return (pending.degraded_line() or "No pending proposals.")
        # D79: the SHARED whole-queue decider
        # (`approval_queue.decide_all_pending`), not a private loop. Two
        # implementations of "decide everything" is exactly how "all" came
        # to mean the self-evolution THIRD of the queue on one seat and the
        # whole union on another — and this copy also lost the one
        # per-item exception guard the shared version has, so a single bad
        # row abandoned every row after it.
        from core.surfaces.correspondents import CorrespondentRegistry
        from tools.controller.approval_queue import decide_all_pending
        try:
            registry = CorrespondentRegistry(
                correspondents_db_path(data_dir))
        except Exception:
            logger.warning("telegram: correspondent registry unavailable",
                           exc_info=True)
            registry = None
        ok_n, fail_n, msgs = decide_all_pending(
            approve=approve, user_id=user_id, home_dir=data_dir,
            instance_id=instance_id, board=board,
            correspondent_registry=registry, task_agent=c.task_agent)
        verb = "approved" if approve else "rejected"
        msgs = list(msgs) + [f"{ok_n} {verb}, {fail_n} failed"]
        return "\n".join(msgs)
    from tools.controller.approval_queue import resolve_pending_target
    match = resolve_pending_target(target, pending)
    if match is None:
        # A `tap-`-shaped target that is not in the OPEN queue is still the
        # tool-approval lane's business: only it can tell the owner whether
        # the ask was already decided, expired, or never existed. A generic
        # "no pending proposal" would flatten those three into one.
        from tools.controller.approval_queue import strip_tap_prefix
        if strip_tap_prefix(target) is not None:
            ok, msg = await _decide_pending_item(
                c.task_agent, {"kind": "tool_approval", "id": target},
                approve=approve, user_id=user_id, data_dir=data_dir,
                instance_id=instance_id, board=board)
            return msg if ok else f"Failed: {msg}"
        _done = self_evolution.last_decision_for_alias(target, user_id=user_id)
        if _done is not None:
            import datetime as _dt
            _when = _dt.datetime.fromtimestamp(
                float(_done["ts"] or 0), _dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
            _verb = "approved" if _done["action"] == "promote" else "rejected"
            return (f"Already {_verb} at {_when} — {_done['kind']}:{_done['item_id']}."
                    " Nothing left to do; see /pending for what is still waiting.")
        return (f"No pending proposal '{target}' — see /pending."
                + (f"\n{pending.degraded_line()}" if pending.degraded_line() else ""))
    ok, msg = await _decide_pending_item(
        c.task_agent, match, approve=approve, user_id=user_id,
        data_dir=data_dir, instance_id=instance_id, board=board)
    return msg if ok else f"Failed: {msg}"


async def asks_verb(c) -> str:
    """`/asks`. Tool-approval asks have their OWN dedicated surface (/pending +
    /approve /reject, tap-<id>) — excluded here so a payment request doesn't
    show twice under two different id shapes."""
    from agents.task.goals.board import ASK_OPEN
    from core.goal_vocab import has_own_surface
    rows = [a for a in c.board.asks(user_id=c.user_id, status=ASK_OPEN)
            if not has_own_surface(a.payload or {})]
    rest = _elsewhere(c, "asks")
    if not rows:
        return "\n".join(x for x in ("No open asks — nothing is blocked on you.", rest) if x)
    lines = [f"{len(rows)} open ask(s):"]
    for a in rows:
        blocks = (a.payload or {}).get("blocks_goal_ids", [])
        lines.append(f"• {a.id} — {a.title}"
                     + (f" (blocks {len(blocks)} goal(s))" if blocks else ""))
        if a.body:
            lines.append(f"   {a.body[:160]}")
    lines.append("Fulfilled one? /fulfill <id> unblocks its goals — add "
                 "your answer after the id and the run is told what you said.")
    if rest:
        lines.append(rest)
    return "\n".join(lines)


async def fulfill_verb(c) -> str:
    """`/fulfill <ask-id> [your answer]`, or a tapped ask option."""
    board, user_id, args = c.board, c.user_id, c.args
    _opt_ask, _opt_letter = _parse_ask_option(c.result.inbound.text)
    if _opt_ask:
        # O14/A4: a tapped option is the owner's TYPED answer: the text is
        # the option the ask stored when it was raised, never a paraphrase.
        _ask = board.get(_opt_ask)
        _opts = ((_ask.payload or {}).get("options") or {}) if (
            _ask is not None and _ask.user_id == user_id) else {}
        if _opt_letter not in _opts:
            return f"No open ask '{_opt_ask}' with option {_opt_letter} — see /asks."
        args = [_opt_ask, f"{_opt_letter}) {_opts[_opt_letter]}"]
    if not args:
        return "Usage: /fulfill <ask-id> [your answer]  (see /asks)"
    # A27/D-followup: the owner's written ANSWER rides with the decision.
    # `GoalBoard.decide_ask(answer=)` stamps it on the ask and carries it
    # into each unblocked goal's retry prompt, so the run is told WHAT the
    # owner said and not only THAT it may proceed. The console has had this
    # seat since the audit; the phone — the seat the owner actually holds —
    # had no way to type an answer at all.
    answer = " ".join(args[1:]).strip()
    ok, unblocked = board.decide_ask(args[0], user_id=user_id, approved=True,
                                     answer=answer or None)
    if not ok:
        return f"No open ask '{args[0]}' — see /asks."
    from agents.task.goals.rail_answers import decision_note
    return (f"✅ Ask fulfilled — {decision_note(board, args[0], unblocked) or 'saved.'}"
            + (f"\nYour answer was passed to the run: {answer[:200]}"
               if answer else ""))


def _allowlist(c):
    from core.surfaces.outbound_allowlist import OutboundAllowlist
    return OutboundAllowlist(surfaces_db_path(c.data_dir))


async def allow_verb(c) -> str:
    """`/allow <surface> <target>`."""
    allowlist = _allowlist(c)
    if len(c.args) < 2:
        return "Usage: /allow <surface> <target>"
    surface, target = c.args[0], c.args[1]
    allowlist.allow(c.user_id, surface, target)
    return f"✅ Allowed {surface}:{target}."


async def deny_verb(c) -> str:
    """`/deny <surface> <target>`."""
    allowlist = _allowlist(c)
    if len(c.args) < 2:
        return "Usage: /deny <surface> <target>"
    surface, target = c.args[0], c.args[1]
    ok = allowlist.revoke(c.user_id, surface, target)
    if not ok:
        return f"No active allowlist entry {surface}:{target}."
    return f"✅ Denied {surface}:{target}."


async def allowlist_verb(c) -> str:
    """`/allowlist` — every entry with its state."""
    rows = _allowlist(c).list(c.user_id)
    if not rows:
        return "No allowlist entries."
    lines = [f"{len(rows)} allowlist entr{'y' if len(rows) == 1 else 'ies'}:"]
    for r in rows:
        note = f" ({r['note']})" if r["note"] else ""
        lines.append(f"• {r['status']} {r['surface']}:{r['target']}{note}")
    return "\n".join(lines)
