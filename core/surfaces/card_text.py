"""A card as text: every seat's fallback, and the body of every button message.

Split from ``cards.py`` (2026-11-18). ``cards.render_text`` is the entry;
:func:`without_taps` is for a seat that draws the card's buttons itself.
"""
from __future__ import annotations

import re
import time
from typing import List

from core.surfaces import cards as C
from core.surfaces.money_report import owner_view

#: The longest command a card prints; ``/card_<id>`` shows all of it. A send or
#: a swap with two full addresses and its bounds fits.
MAX_COMMAND_CHARS = 240

_TAP = re.compile(r"/card_[0-9a-f]{10}_(?:ok|no|re|[1-6])\b")
_LABEL = re.compile(r"[A-Za-z][A-Za-z —-]*:")


def without_taps(text: str) -> str:
    """``text`` without the lines that only spell a card's taps
    (``Confirm: /card_<id>_ok   Cancel: …``, ``Pick: …``). For a message that
    carries the SAME taps as buttons: the line repeats them, 80 characters of
    ids. A line with any other word stays, and so does a text with no buttons."""
    kept = []
    for line in (text or "").splitlines():
        if _TAP.search(line) and not _LABEL.sub("", _TAP.sub("", line)).strip():
            continue
        kept.append(line)
    return "\n".join(kept)


def render(card: "C.Card", *, full: bool = False) -> str:
    """Each action's typed token is in the text, so nothing depends on a
    button rendering.

    A money report reads as ``money_report.owner_view`` (the stored text keeps
    every line), and a DECIDED quote shows its result, not its quote again.

    ``full`` (``/card_<id>``, 096 C4): the whole stored title and body. Without
    it a title over ``MAX_TITLE_CHARS`` or a body over ``MAX_BODY_CHARS`` is
    cut WITH a marker that names the reopen tap."""
    lines: List[str] = []
    title = " ".join((card.title or "").split())
    body = (card.body or "").rstrip()
    decided = card.state != C.S_OPEN
    if not full and card.kind == C.KIND_QUOTE:
        body = "" if decided and card.result else owner_view(body)
    cut = False
    if not full and len(title) > C.MAX_TITLE_CHARS:
        title, cut = C._short(title, C.MAX_TITLE_CHARS), True
    hidden = 0
    if not full and len(body) > C.MAX_BODY_CHARS:
        hidden = len(body) - C.MAX_BODY_CHARS
        body, cut = body[:C.MAX_BODY_CHARS].rstrip(), True
    head = title
    if card.origin == C.ORIGIN_AGENT:
        head = f"🤖 From the agent — {head}"
    lines.append(head)
    if body:
        lines.append("")
        lines.append(body + (f"… (+{hidden} chars)" if hidden else ""))
    if cut:
        tap = C.read_token(card.card_id)
        if tap:
            lines.append(f"(cut short — the whole card: {tap})")
    if card.kind in (C.KIND_CHOICE, C.KIND_BUILDER) and card.options:
        lines.append("")
        for i, opt in enumerate(card.options, start=1):
            lines.append(f"  {i}. {opt}")
    if card.kind in (C.KIND_QUOTE, C.KIND_PROPOSAL, C.KIND_JOB, C.KIND_SETTING) and card.line:
        command = card.confirm_line if card.kind == C.KIND_QUOTE else card.line
        tap = C.read_token(card.card_id)
        if not full and command in title:
            command = ""                  # a setting's title already IS its line
        elif not full and len(command) > MAX_COMMAND_CHARS and tap:
            # A job's line repeats its whole task prose; the body names the grant.
            command = f"{command[:MAX_COMMAND_CHARS].rstrip()}… (the whole command: {tap})"
        if command:
            lines.append("")
            lines.append(f"Command: {command}")
    lines.append("")
    if not decided:
        cid = card.card_id
        tok = C.card_token
        if card.kind == C.KIND_QUOTE:
            mins = max(1, int((card.expires_at - time.time()) // 60))
            lines.append(f"Confirm: {tok(cid, 'ok')}   Refresh: {tok(cid, 're')}"
                         f"   Cancel: {tok(cid, 'no')}")
            lines.append(f"(valid for {mins} min, one use)")
        elif card.kind in C.LINE_KINDS:
            lines.append(f"Confirm: {tok(cid, 'ok')}   Cancel: {tok(cid, 'no')}")
            lines.append("(Confirm runs the command above — nothing is written before)")
        elif (card.kind == C.KIND_PROPOSAL and card.origin == C.ORIGIN_SYSTEM
              and C.is_ack_step(card.args)):
            lines.append(f"I understand: {tok(cid, 're')}   Cancel: {tok(cid, 'no')}")
            lines.append("(nothing moves from this card — the next quote has the Confirm)")
        elif card.kind == C.KIND_PROPOSAL:
            lines.append(f"Get a quote: {tok(cid, 're')}   Cancel: {tok(cid, 'no')}")
            lines.append("(nothing moves from this card — the quote comes first)")
        else:
            picks = "  ".join(tok(cid, str(i)) for i in range(1, len(card.options) + 1))
            if picks:
                lines.append("Pick: " + picks)
            if card.kind == C.KIND_BUILDER:
                lines.append(f"Cancel: {tok(cid, 'no')}")
    else:
        lines.append(C._STATE_WORDS.get(card.state, card.state))
        if card.answer:
            lines.append(f"Picked: {card.answer}")
        if card.result and not full:
            # The result message carries the report; the card keeps the receipt.
            from core.surfaces.card_groups import receipt_facts
            facts = receipt_facts(card.result) if card.state == C.S_DONE else ""
            lines.append(facts or C._short_block(owner_view(card.result), 700))
        elif card.result:
            lines.append(C._short_block(card.result, 700))
    return "\n".join(lines)
