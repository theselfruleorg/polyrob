"""Text renderers for the Inbox and the book (043 D1).

The twin of :mod:`core.status_render`: the composition lives in
:mod:`core.surfaces.inbox` (and, for the book, ``tools.defi.book.read_book``),
and every TEXT seat renders from here — the REPL's ``/inbox`` and ``/book``,
Telegram's, and ``polyrob wallet book``. One renderer, so the terminal and the
phone cannot start describing the same list differently.

The shape is ``docs/design/040/cli/inbox-80.txt`` (three states) and
``money-book-80.txt`` / ``money-disagree-80.txt``. **Eighty columns**, because
that is the width a terminal is, and because a line that wraps is a line a
person re-reads.

Two things carried over from the composition, and they are the reason this file
is not a formatter:

* "Nothing needs you" is printed only when **every** source answered. A refused
  source is a listed entry with its own mark, and the footer names it.
* The count is **decisions**. An informational row is printed under "Not
  blocking, listed and not counted" and is not in the number.

The remedy under each item differs by SEAT (the REPL decides a proposal with
``/pending approve <kind> <id>``; Telegram with ``/approve <id>``), so the
remedy table is an argument rather than a hardcode. What a card SAYS never
differs.
"""
from __future__ import annotations

import textwrap
import time
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from core.surfaces.inbox import KIND_UNREADABLE, SOURCE_LABELS

WIDTH = 80

#: Marks, from the mockup. ``!`` is a decision, ``?`` is a list that refused.
MARK_BLOCKING = "!"
MARK_UNREADABLE = "?"

#: ``kind -> ((word, command-template), …)``. ``{id}`` is the item id.
RemedyTable = Mapping[str, Sequence[Tuple[str, str]]]

#: The REPL. ``/pending approve <kind> <id>`` is its decider; ``/approve`` there
#: is the approval-GATES verb and means something else entirely.
REPL_REMEDIES: Dict[str, Sequence[Tuple[str, str]]] = {
    "app": (("yes", "/apps approve {id}"), ("no", "/apps reject {id}")),
    "ask": (("mark it done", "/fulfill {id}"),),
    "tool_approval": (("yes", "/pending approve tool_approval {id}"),
                      ("no", "/pending reject tool_approval {id}")),
    "correspondent": (("yes", "/pending approve correspondent {id}"),
                      ("no", "/pending reject correspondent {id}")),
    KIND_UNREADABLE: (("look", "/inbox"), ("why", "/logs")),
    "": (("yes", "/pending approve {kind} {id}"),
         ("no", "/pending reject {kind} {id}")),
}

#: The `polyrob` CLI. Its verbs are `polyrob owner …`, not slash commands —
#: `polyrob owner inbox` used to render :data:`REPL_REMEDIES`, so every card
#: under it told the operator to type `/pending approve …` at a shell prompt,
#: which is not a command on any seat they were looking at (C21).
CLI_REMEDIES: Dict[str, Sequence[Tuple[str, str]]] = {
    "app": (("yes", "polyrob apps approve {id}"),
            ("no", "polyrob apps reject {id}")),
    "ask": (("mark it done", "polyrob owner fulfill {id}"),),
    "tool_approval": (("yes", "polyrob owner promote tool_approval {id}"),
                      ("no", "polyrob owner reject tool_approval {id}")),
    "correspondent": (("yes", "polyrob owner promote correspondent {id}"),
                      ("no", "polyrob owner reject correspondent {id}")),
    KIND_UNREADABLE: (("look", "polyrob owner inbox"),
                      ("why", "polyrob doctor --full")),
    "": (("yes", "polyrob owner promote {kind} {id}"),
         ("no", "polyrob owner reject {kind} {id}")),
}

#: Telegram. Its ``/approve`` IS the decider.
#:
#: 2026-09-15: the default row was ``/approve {id}``, and a chat client links
#: only the VERB — so the one tappable thing on the page was the half that does
#: nothing, and the owner copied an id by hand off a phone. ``{tap_approve}`` /
#: ``{tap_reject}`` render the single auto-linked token instead
#: (``core.self_evolution.pending_tap_token``), which is also what ``/pending``
#: and the proposal notice render, so all three agree.
CHAT_REMEDIES: Dict[str, Sequence[Tuple[str, str]]] = {
    "app": (("yes", "/apps approve {id}"), ("no", "/apps reject {id}")),
    "ask": (("mark it done", "/fulfill {id}"),),
    KIND_UNREADABLE: (("look", "/inbox"), ("why", "/status")),
    "": (("yes", "{tap_approve}"), ("no", "{tap_reject}")),
}


def age(seconds: Optional[float], now: Optional[float] = None) -> str:
    """``2h`` / ``19h`` / ``6d`` — or ``""`` when the record carries no stamp.

    An unstamped record is common (a quarantined proposal has no reliable age
    on disk), and inventing "just now" for it would be the small confident lie
    this whole surface exists to avoid.
    """
    if not seconds:
        return ""
    delta = max(0.0, (now if now is not None else time.time()) - float(seconds))
    if delta < 3600:
        return f"{max(1, int(delta // 60))}m"
    if delta < 48 * 3600:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


def _wrap(text: str, indent: str, width: int = WIDTH) -> list:
    text = " ".join(str(text or "").split())
    if not text:
        return []
    return textwrap.wrap(text, width=width, initial_indent=indent,
                         subsequent_indent=indent, break_long_words=False,
                         break_on_hyphens=False) or []


def _headline(mark: str, title: str, right: str, width: int = WIDTH) -> str:
    """``  ! <title>                      waiting 2h`` — right-aligned age."""
    left = f"  {mark} {' '.join(str(title or '').split())}"
    if not right:
        return left[:width]
    room = width - len(right) - 1  # one space, minimum, between the two
    if len(left) > room:
        left = left[:max(0, room - 1)].rstrip() + "…"
    gap = max(1, width - len(left) - len(right))
    return f"{left}{' ' * gap}{right}"


def _remedy_line(item: Mapping[str, Any], table: RemedyTable,
                 width: int = WIDTH) -> str:
    """The commands that decide this item, on THIS seat.

    A non-blocking row falls back to nothing rather than to the generic
    approve/reject pair: it is listed because you may want to act, not because
    a decision is owed, and offering "yes/no" on it would make the page's own
    distinction meaningless.
    """
    kind = str(item.get("kind") or "")
    verbs = table.get(kind)
    if verbs is None:
        verbs = table.get("") or () if item.get("blocking") else ()
    try:
        from core.self_evolution import pending_tap_token
        as_item = {"kind": kind, "id": item.get("id") or ""}
        tap_approve = pending_tap_token("approve", as_item)
        tap_reject = pending_tap_token("reject", as_item)
    except Exception:
        # Fail-open to the typed form: a missing token must cost a tap, never
        # the remedy line itself.
        tap_approve, tap_reject = "/approve", "/reject"
    parts = []
    for word, command in verbs:
        parts.append(f"{word}  "
                     + command.format(id=item.get("id") or "", kind=kind,
                                      tap_approve=tap_approve,
                                      tap_reject=tap_reject))
    if not parts:
        return ""
    line = "      " + "      ".join(parts)
    return line if len(line) <= width else "      " + "\n      ".join(parts)


def _item_block(item: Mapping[str, Any], table: RemedyTable, *,
                mark: str, now: Optional[float], width: int) -> list:
    stamp = age(item.get("created_at"), now)
    right = f"waiting {stamp}" if stamp else ""
    if item.get("unreadable"):
        right = ""
    lines = [_headline(mark, item.get("title"), right, width)]
    body = item.get("body") or ""
    if item.get("unreadable"):
        body = ("I could not open this list. The store did not answer, so I "
                "do not know whether anything is waiting in it. A dash is not "
                "the same as none.")
    lines += _wrap(body, "    ", width)
    lines += _wrap(item.get("meta"), "    ", width)
    remedy = _remedy_line(item, table, width)
    if remedy:
        lines.append(remedy)
    return lines


def _sources_footer(body: Mapping[str, Any], width: int) -> list:
    sources = body.get("sources") or {}
    refused = list(body.get("unreadable_sources") or ())
    read = [SOURCE_LABELS.get(n, n) for n in sources if n not in refused]
    lines = []
    if read:
        lines += _wrap("Read " + ", ".join(read) + ".", "  ", width)
    if refused:
        lines += _wrap("Could not read "
                       + ", ".join(SOURCE_LABELS.get(n, n) for n in refused)
                       + ".", "  ", width)
    elif read:
        lines += _wrap("All of them answered.", "  ", width)
    return lines


def _more_line(hidden: int, width: int) -> str:
    """What a truncated section leaves out, and how to see the rest.

    ⚠️ ``_wrap(...)[0]`` took only the FIRST line, so at a phone's 60 columns the
    sentence announcing a truncation truncated itself.
    """
    what = "one more is" if hidden == 1 else f"{hidden} more are"
    return "\n".join(_wrap(
        f"{what} waiting here and not shown. Ask for the whole list.",
        "  ", width))


def render_inbox(body: Mapping[str, Any], *, remedies: RemedyTable = REPL_REMEDIES,
                 limit: Optional[int] = None, width: int = WIDTH,
                 now: Optional[float] = None) -> str:
    """The Inbox at *width* columns, in one of its three states."""
    items = list(body.get("items") or ())
    listed = list(body.get("not_blocking") or ())
    count = int(body.get("count") or 0)
    refused = list(body.get("unreadable_sources") or ())
    # ⚠️ A truncated list SAYS it was truncated. A silent cut is the same
    # failure shape as a silently dropped source: the reader believes they have
    # seen everything, and the count above them stops matching what is on
    # screen.
    hidden_items = hidden_listed = 0
    if limit:
        keep = max(1, int(limit))
        hidden_items = max(0, len(items) - keep)
        hidden_listed = max(0, len(listed) - keep)
        items = items[:keep]
        listed = listed[:keep]

    out: list = []
    if refused:
        warning = _wrap("This list is incomplete. I could not read "
                        + ", ".join(SOURCE_LABELS.get(n, n) for n in refused)
                        + ", so there may be more waiting than what is below. "
                        "Decide those on Telegram until this is readable again.",
                        "    ", width)
        # The warning leads, marked, and every continuation line hangs under it.
        out += ["  ⚠ " + warning[0].strip()] + warning[1:]
    elif not items:
        out += ["  Nothing needs you.", "",
                "  I can only say that because every list answered. If one had not,",
                "  I would have said so instead."]
    if items:
        if not refused:
            lead = ("  One thing needs you, blocking first. I keep working on the rest."
                    if count == 1 else
                    f"  {count} things need you, blocking first. "
                    "I keep working on the rest.")
            out += _wrap(lead.strip(), "  ", width)
        for item in items:
            mark = (MARK_UNREADABLE if item.get("unreadable") else MARK_BLOCKING)
            out += [""] + _item_block(item, remedies, mark=mark, now=now,
                                      width=width)
        if hidden_items:
            out += ["", _more_line(hidden_items, width)]
    if listed:
        out += ["", "  Not blocking, listed and not counted:"]
        for item in listed:
            out += [""] + _item_block(item, remedies, mark=" ", now=now,
                                      width=width)
        if hidden_listed:
            out += ["", _more_line(hidden_listed, width)]
    footer = _sources_footer(body, width)
    if footer:
        out += [""] + footer
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# the book
# --------------------------------------------------------------------------- #

#: ``tools.defi.book.read_book``'s per-chain verdicts, in words.
_VERDICT_WORDS = {
    "clean": "The ledger and the chains agree.",
    "unverified": "I could not verify the book.",
    "no_ledger": "There is no position ledger to check the chains against.",
    "disagreement": "My ledger and the chain DISAGREE.",
}

_CHAIN_WORDS = {
    "clean": "agrees",
    "unverified": "could not be verified",
    "no_ledger": "has nothing written down to check",
    "disagreement": "disagrees",
}

#: The reconcile report's own lists, in the order a person needs them, with the
#: words ``tools.defi.reconcile`` already uses for each.
_ISSUE_SECTIONS = (
    ("unbacked", "written down, not held"),
    ("mismatched", "a different size on the chain"),
    ("unexplained", "held on the chain, not written down"),
    ("unreadable_rows", "rows I could not parse"),
    ("unknown", "could not verify — unknown is not zero"),
)


def render_book(body: Mapping[str, Any], *, width: int = WIDTH,
                now: Optional[float] = None) -> str:
    """The book at *width* columns: one verdict, then what disagrees.

    ⚠️ No per-position price table. ``read_book`` carries a verdict and the
    reconcile report's own already-human lines; it does not carry a per-position
    value, and a table of numbers this reader does not have would be a mockup,
    not a report.
    """
    if body.get("error"):
        return "\n".join(_wrap(str(body["error"]), "  ", width))
    chains = dict(body.get("chains") or {})
    verdict = str(body.get("verdict") or "").lower()
    out = _wrap(_VERDICT_WORDS.get(verdict, "I could not read the book."),
                "  ", width)

    stamp = age(body.get("checked_at"), now)
    checked = ("Checked " + stamp + " ago on " if stamp else "Checked on ")
    names = ", ".join(sorted(chains)) or "no chain at all"
    out += _wrap(checked + names + ".", "  ", width)

    for name in sorted(chains):
        entry = chains[name] or {}
        word = _CHAIN_WORDS.get(str(entry.get("verdict") or "").lower(),
                                "is in a state I do not recognise")
        line = f"    {name} {word}"
        if entry.get("error"):
            line += f" — {' '.join(str(entry['error']).split())}"
        out += _wrap(line.strip(), "    ", width)
        report = entry.get("report") or {}
        for key, label in _ISSUE_SECTIONS:
            rows = report.get(key) or []
            for row in rows:
                out += _wrap(f"{label}: {row}", "      ", width)

    if verdict == "disagreement":
        out += [""]
        out += _wrap("The chain is the truth; the ledger is my memory of it, "
                     "and the memory is wrong. I will not trade, rewrite the "
                     "ledger, or say anything in public about my positions "
                     "until this is settled.", "  ", width)
    return "\n".join(out)
