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

from core.surfaces.more_store import clip
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
                          board=board, task_agent=task_agent,
                          rev=item.get("seat_rev"))


async def pending_verb(c) -> str:
    """`/pending`. ONE union — the same one `/approve` decides over (2026-09-15).
    Chat used to build this join by hand while the bare `/approve` shortcut read
    only the first of the three sources; see `approval_queue.all_pending`."""
    from core import self_evolution
    from core.pending_review import summary_line
    pending = _pending_set(c.user_id, c.data_dir, c.instance_id, c.board)
    if c.args and c.args[0].lower() != "all":
        return _review_one(c, pending, c.args[0])
    rest = _elsewhere(c, "pending")
    if not pending.items:
        return "\n".join(x for x in (pending.degraded_line() or "No pending approvals.",
                                       rest) if x)
    # 096 W3/A8: a self-evolution item reads as the notice does (what · why ·
    # changes · taps), and a linked pair is ONE entry, not two halves.
    by_key = {f"{it['kind']}:{it['id']}": it for it in pending.items}
    shown: set = set()
    rows = []
    for it in pending.items:
        key = f"{it['kind']}:{it['id']}"
        if key in shown:
            continue
        shown.update({key, it.get("linked") or ""})
        rows.append(it)
    lines = [f"{len(rows)} pending approval(s):"]
    for it in rows:
        if it.get("kind") in self_evolution.PENDING_KIND_LABELS:
            lines.extend(self_evolution.notice_item_lines(
                it, partner=by_key.get(it.get("linked") or "")))
            continue
        lines.append(f"• {it['kind']}:{it['id']} — {summary_line(it)}")
        lines.append(f"   {self_evolution.pending_tap_token('approve', it)} · "
                     f"{self_evolution.pending_tap_token('reject', it)} · "
                     f"read it: {self_evolution.pending_tap_token('pending', it)}")
    lines.append("Everything at once: /approve_all")
    if pending.degraded_line():
        lines.append(pending.degraded_line())
    if rest:
        lines.append(rest)
    return "\n".join(lines)


def _review_one(c, pending, target: str) -> str:
    """`/pending <id>` or the tapped `/pending_p_<hex>`: ONE item, in full.

    The notice and the list can only summarize; this is where the owner reads
    what a skill update changes (a diff against the live skill) before the tap
    that decides it. A queued tool approval or a contact has no draft body:
    its whole record is its list line."""
    from core import self_evolution
    from tools.controller.approval_queue import resolve_pending_target
    match = resolve_pending_target(target, pending)
    if match is None:
        return f"No pending item '{target}' — see /pending."
    if match.get("kind") not in self_evolution.PENDING_KIND_LABELS:
        from core.pending_review import summary_line
        taps = (f"{self_evolution.pending_tap_token('approve', match)}   "
                f"{self_evolution.pending_tap_token('reject', match)}")
        # 096 B3: a queued tool approval opens its STORED full card (the whole
        # tweet / DM / email body, every money field), not the one-line summary.
        card = str(match.get("card") or "")
        if card:
            return f"{card}\n{taps}"
        if match.get("kind") == "correspondent":
            return _contact_review(c, match)
        if match.get("kind") == "signer_approval":
            # 096 C2: the signer's own review (op, chain, priced risk, the
            # whole summary), not the 220-char list line.
            from core.signer.approvals import review as signer_review
            ok, text = signer_review(str(match["id"]))
            body = text if ok else f"{match['kind']}:{match['id']} — {match.get('preview') or ''}\n({text})"
            return f"{body}\n{taps}"
        return f"{match['kind']}:{match['id']} — {summary_line(match)}\n{taps}"
    ok, text = self_evolution.review(match["kind"], str(match["id"]), user_id=c.user_id,
                                     home_dir=c.data_dir, instance_id=c.instance_id)
    return text if ok else f"Could not open it: {text}"


def _contact_review(c, match: dict) -> str:
    """096 C1: a pending contact opens as its decision notice — who, why (the
    stored provenance + session), the first message the agent sent (from the
    conversation store), and the taps. Fail-open to "not recorded"."""
    from core.surfaces.seed import build_contact_notice
    surface, _, address = str(match.get("id") or "").partition(":")
    first = None
    try:
        from surfaces.telegram.contacts_ops import _store
        store = _store(getattr(c, "container", None), str(c.data_dir))
        row = (store.first_outbound(str(c.user_id), surface, address)
               if store is not None else None)
        if row is not None:
            subj = str(row.get("subject") or "")
            body = str(row.get("body") or "")
            first = f"Subject: {subj}\n\n{body}" if subj else body
    except Exception as exc:  # say so in the notice; never drop the section silently
        logger.warning("contact review: first message unreadable", exc_info=True)
        first = f"unavailable ({type(exc).__name__})"
    return build_contact_notice(match, surface=surface, address=address,
                                session_id=str(match.get("session_id") or ""),
                                provenance=str(match.get("provenance") or ""),
                                first_message=first, more_tenant=str(c.user_id))


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
            from core.pending_review import summary_line
            _preview = summary_line(_it)
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
    if c.args:
        return _ask_one(c, rows, str(c.args[0]))
    rest = _elsewhere(c, "asks")
    if not rows:
        return "\n".join(x for x in ("No open asks — nothing is blocked on you.", rest) if x)
    from core.surfaces.tappable import read_token
    lines = [f"{len(rows)} open ask(s):"]
    for a in rows:
        blocks = (a.payload or {}).get("blocks_goal_ids", [])
        read = read_token("/ask", a.id)
        lines.append(f"• {a.id} — {a.title}"
                     + (f" (blocks {len(blocks)} goal(s))" if blocks else ""))
        if a.body:
            body = str(a.body)
            # 096 C3: the Why was cut at 160 with no marker. Now 600, and the
            # whole ask one tap away.
            if len(body) > _ASKS_BODY_CHARS:
                body = (body[:_ASKS_BODY_CHARS].rstrip()
                        + f"… (+{len(body) - _ASKS_BODY_CHARS} chars"
                        + (f": {read})" if read else ")"))
            lines.append(f"   {body}")
        taps = _ask_option_taps(a)
        if taps:
            lines.append(f"   Tap an answer: {taps}")
        if read:
            lines.append(f"   read it all: {read}")
    lines.append("Fulfilled one? /fulfill <id> unblocks its goals — add "
                 "your answer after the id and the run is told what you said.")
    if rest:
        lines.append(rest)
    return "\n".join(lines)


#: `/asks` shows this much of each ask's Why; `/ask_<id>` shows all of it.
_ASKS_BODY_CHARS = 600


def _ask_option_taps(ask: Any) -> str:
    """``A) /fulfill_<id>_a · B) …`` for an ask that stored options (O14)."""
    from core.surfaces.tappable import ASK_OPTION_LETTERS, ask_option_token
    opts = (ask.payload or {}).get("options") or {}
    return " · ".join(f"{letter}) {ask_option_token(ask.id, letter)}"
                      for letter in ASK_OPTION_LETTERS if letter in opts)


def _ask_one(c, rows: list, ref: str) -> str:
    """`/asks <id>` or the tapped `/ask_<id>`: ONE open ask in full — the
    question, the whole Why, each option with its tap. A read, never a decision."""
    ref = ref.strip().lower()
    matches = [a for a in rows if str(a.id).lower() == ref] or [
        a for a in rows if str(a.id).lower().startswith(ref)]
    if not matches:
        return f"No open ask '{ref}' — see /asks."
    if len(matches) > 1:
        return f"'{ref}' matches {len(matches)} open asks — use more characters (see /asks)."
    a = matches[0]
    payload = a.payload or {}
    blocks = payload.get("blocks_goal_ids", [])
    lines = [f"❓ Ask {a.id}" + (f" (blocks {len(blocks)} goal(s))" if blocks else ""),
             str(a.title or "")]
    if a.body:
        lines += ["", "Why: " + str(a.body)]
    opts = payload.get("options") or {}
    if opts:
        from core.surfaces.tappable import ASK_OPTION_LETTERS, ask_option_token
        lines.append("")
        for letter in ASK_OPTION_LETTERS:
            if letter in opts:
                lines.append(f"{letter}) {opts[letter]} — {ask_option_token(a.id, letter)}")
    lines += ["", f"Answer here in chat, or /fulfill {a.id} <your answer>."]
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
            + (f"\nYour answer was passed to the run: {clip(answer, 200)}"
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


async def pair_verb(c) -> str:
    """`/pair [list|approve <surface> <code>|revoke <surface> <id>]` — DM pairing."""
    from core.pairing import pair_verb as _pair, store_for
    return _pair(list(c.args), store=store_for(c.data_dir))


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
