"""096 W7 — one pending-item renderer for every seat (R4).

Telegram's ``/pending`` and the proactive notice render a pending item through
``core.self_evolution.notice_item_lines`` (what · Why · Changes) and open one
through ``core.self_evolution.review`` (a diff against the live text). The CLI,
the REPL and the console used to print the raw ``preview`` instead — a
flattened frontmatter line — and opened a draft with ``show()``, which has no
diff. This module is the seam those seats share, so a seat may lay an item out
differently but may not show less.

The only thing that differs per seat is HOW the owner decides: a chat seat
taps a token, the CLI types a command, the REPL types a slash command, the
console clicks a button. :func:`decide_line` is that one difference.

Pure over the items it is handed (``tools.controller.approval_queue.all_pending``
builds them; ``core`` may not import ``tools``). Never raises out of a render:
an item it cannot describe falls back to its own preview.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SEAT_CHAT = "chat"
SEAT_CLI = "cli"
SEAT_REPL = "repl"
SEAT_CONSOLE = "console"

#: Labels for the kinds that ride the union but are not self-evolution kinds.
_OTHER_LABELS = {"correspondent": "contact", "tool_approval": "approval",
                 "signer_approval": "signer"}


def kind_label(kind: str) -> str:
    """The short label a list shows before an item."""
    from core import self_evolution
    if kind in _OTHER_LABELS:
        return _OTHER_LABELS[kind]
    if kind in self_evolution.PENDING_KIND_LABELS:
        return self_evolution.pending_kind_label(kind)
    return kind or "item"


def is_self_evolution(item: Dict[str, Any]) -> bool:
    from core import self_evolution
    return item.get("kind") in self_evolution.PENDING_KIND_LABELS


def fold_linked(items: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """``[(item, partner)]`` — a linked pair (095 W10) is ONE row, decided by
    one pair of taps, exactly as the Telegram notice folds it."""
    by_key = {f"{it.get('kind')}:{it.get('id')}": it for it in items}
    shown: set = set()
    rows = []
    for it in items:
        key = f"{it.get('kind')}:{it.get('id')}"
        if key in shown:
            continue
        shown.update({key, it.get("linked") or ""})
        rows.append((it, by_key.get(it.get("linked") or "")))
    return rows


def decide_line(item: Dict[str, Any], seat: str, *, both: bool = False) -> str:
    """How the owner decides ``item`` on ``seat`` — the one per-seat difference.
    ``""`` on the console, whose card carries buttons."""
    from core.self_evolution import decision_id, pending_tap_token
    kind = item.get("kind", "")
    # 098 CO-19: a typed seat decides ``<id>@<rev>`` — the revision shown here.
    item_id = decision_id(item)
    lead = "Decide both: " if both else ""
    if seat == SEAT_CHAT:
        return (f"{lead}{pending_tap_token('approve', item)} · "
                f"{pending_tap_token('reject', item)}")
    if seat == SEAT_CLI:
        return (f"{lead}polyrob owner promote {kind} {item_id}   ·   "
                f"polyrob owner reject {kind} {item_id}")
    if seat == SEAT_REPL:
        return (f"{lead}/pending approve {kind} {item_id}   ·   "
                f"/pending reject {kind} {item_id}")
    return ""


def read_line(item: Dict[str, Any], seat: str) -> str:
    """How the owner opens the whole of ``item`` on ``seat``."""
    from core.self_evolution import pending_tap_token
    kind, item_id = item.get("kind", ""), item.get("id", "")
    if seat == SEAT_CHAT:
        return f"read it: {pending_tap_token('pending', item)}"
    if seat == SEAT_CLI:
        return f"read it: polyrob owner show-pending {kind} {item_id}"
    if seat == SEAT_REPL:
        return f"read it: /pending show {kind} {item_id}"
    return ""


def item_lines(item: Dict[str, Any], partner: Optional[Dict[str, Any]] = None, *,
               seat: str = SEAT_CHAT) -> List[str]:
    """ONE pending row as the owner decides it: what · Why · Changes for a
    self-evolution item (the notice's own lines), the shared one-line summary
    for any other kind — then this seat's decide and read lines."""
    from core.pending_review import summary_line
    if is_self_evolution(item):
        from core.self_evolution import notice_item_lines
        try:
            lines = notice_item_lines(item, partner=partner)
        except Exception:
            logger.debug("pending_seats: notice lines failed", exc_info=True)
            lines = [f"• {kind_label(item.get('kind', ''))} {item.get('id')} — "
                     f"{summary_line(item)}"]
        if seat == SEAT_CHAT:
            return lines
        # The last notice line is the chat tap line; this seat has its own verbs.
        lines = lines[:-1] if len(lines) > 1 else lines
    else:
        lines = [f"• {kind_label(item.get('kind', ''))} {item.get('kind')}:{item.get('id')}"
                 f" — {summary_line(item)}"]
    tail = " · ".join(x for x in (decide_line(item, seat, both=bool(partner)),
                                  read_line(item, seat)) if x)
    if tail:
        lines.append(f"   {tail}")
    return lines


def list_lines(items: List[Dict[str, Any]], *, seat: str) -> List[str]:
    """Every row of a pending list, linked pairs folded."""
    out: List[str] = []
    for it, partner in fold_linked(items):
        out.extend(item_lines(it, partner, seat=seat))
    return out


def review_one(item: Dict[str, Any], *, user_id: str, home_dir: Any, instance_id: str,
               seat: str = SEAT_CHAT, skill_manager: Any = None) -> Tuple[bool, str]:
    """The whole of ONE pending item, for ``seat``. A self-evolution draft is
    ``self_evolution.review`` (Why, Changes, the diff, both halves of a linked
    pair); a tool approval its stored full card; a signer request the signer's
    own review; any other kind its summary. Ends with this seat's decide line."""
    from core.pending_review import summary_line
    kind, item_id = str(item.get("kind") or ""), str(item.get("id") or "")
    both = bool(item.get("linked"))
    if is_self_evolution(item):
        from core import self_evolution
        ok, text = self_evolution.review(kind, item_id, user_id=user_id,
                                         home_dir=home_dir, instance_id=instance_id,
                                         skill_manager=skill_manager)
        if not ok:
            return ok, text
        if seat != SEAT_CHAT:
            # review() closes with the chat tap line; swap it for this seat's.
            head, sep, last = text.rpartition("\n")
            if sep and last.startswith("Decide"):
                text = head
            line = decide_line(item, seat, both=both)
            text = f"{text}\n{line}" if line else text
        return True, text
    if kind == "signer_approval":
        try:
            from core.signer.approvals import review
            ok, body = review(item_id)
        except Exception as e:
            ok, body = False, f"could not read the signer's review: {e}"
        if not ok:
            body = f"{kind}:{item_id} — {summary_line(item)}\n({body})"
    else:
        body = str(item.get("card") or "") or f"{kind}:{item_id} — {summary_line(item)}"
    line = decide_line(item, seat)
    return True, f"{body}\n{line}" if line else body


def find_item(items: List[Dict[str, Any]], kind: str, item_id: str) -> Optional[Dict[str, Any]]:
    """The item ``kind:item_id`` of a pending list, or None."""
    for it in items:
        if str(it.get("kind")) == str(kind) and str(it.get("id")) == str(item_id):
            return it
    return None


__all__ = ["SEAT_CHAT", "SEAT_CLI", "SEAT_REPL", "SEAT_CONSOLE", "kind_label",
           "fold_linked", "decide_line", "read_line", "item_lines", "list_lines",
           "review_one", "find_item"]
