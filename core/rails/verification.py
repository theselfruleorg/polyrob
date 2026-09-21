"""The ONE per-rail verification table (057 WS-E / R4).

The S4 class: the agent posts to a Telegram channel, the send returns a
`message_id`, and then it goes looking for the post to "confirm" it — reads the
room ledger, finds nothing (Telegram never echoes a bot's own channel posts back
as updates), and reports the delivery as *unconfirmed*. It spent a turn proving
a fact it already held, and then filed the wrong answer.

The opposite failure is the same shape: a rail where the receipt is NOT the
proof (an X post can be fetched back; an on-chain transaction must be read back)
and the agent treats the call returning without an exception as done.

So the rule cannot be "trust the receipt" or "always re-read". It is per rail,
and until now it existed in exactly one place — a bullet in
`x-engagement/SKILL.md`, corrupted — plus some prose inside `room_read`'s
output. This module is that rule, once, in code:

- :data:`VERIFICATION` — the table. One row per rail: what counts as **proof**,
  the **method** for checking it, and the **note** that names the trap.
- :func:`verification_line` — the one-line render appended to a tool result, so
  the proof arrives WITH the receipt instead of being looked up afterwards.
- :func:`render_guide` — the same table as `docs/guide/rails-verification.md`.
- the `x-engagement` skill's "Proof per rail" section cites the guide.

⚠️ This table states what CAN be proved, not that anything WAS. A rail whose
proof is the receipt still needs the receipt; a rail whose proof is a read-back
still needs the read-back. Nothing here marks anything verified.

Pure core: stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

#: Where the long-form version of this table lives (cited in rendered lines so a
#: reader can get the whole rule, not just their own rail's row).
GUIDE_PATH = "docs/guide/rails-verification.md"


@dataclass(frozen=True)
class RailVerification:
    """One rail's answer to "how do I know that landed?"."""

    rail: str
    title: str
    #: Short noun for the thing the call hands back, e.g. ``telegram receipt``.
    receipt: str
    #: WHAT counts as proof.
    proof: str
    #: HOW to check it (or why it cannot be checked).
    method: str
    #: The trap — the specific wrong conclusion this row exists to prevent.
    note: str


def _row(rail, title, receipt, proof, method, note) -> RailVerification:
    return RailVerification(rail=rail, title=title, receipt=receipt, proof=proof,
                            method=method, note=note)


#: The table. Keys are RAIL ids, not tool names — `x_post` and `x_reply` are
#: separate rows because their read-back differs (your own timeline vs. someone
#: else's thread), while `message(surface='telegram')` maps to `telegram_channel`
#: or `telegram_group` depending on where it lands.
VERIFICATION: Dict[str, RailVerification] = {
    "x_post": _row(
        "x_post", "X — your own post", "x status url",
        "the status URL (or post id) the write call returned",
        "open the returned status URL; it is a public page and it is readable "
        "by the same browser session that posted it",
        "a saved draft, a plan, or 'ready to post' is NOT a post — only the "
        "returned URL/id proves publication",
    ),
    "x_reply": _row(
        "x_reply", "X — a reply under someone else's post", "x status url",
        "the status URL (or post id) the reply call returned",
        "open the returned status URL; the reply is a status of its own",
        "the API tier returns 403 for a non-mentioner, so a reply that did not "
        "come back with a URL did not happen — the browser rail is the one that "
        "can post it",
    ),
    "telegram_channel": _row(
        "telegram_channel", "Telegram — a channel you post to", "telegram receipt",
        "the send receipt (API 200 + message_id) IS the verification",
        "read the message_id off the send result — there is nothing else to read",
        "the bot cannot read its own channel posts: Telegram never delivers them "
        "back as updates, so an empty room ledger says NOTHING about whether your "
        "post rendered. Do not call a delivered post unconfirmed because you "
        "cannot find it",
    ),
    "telegram_group": _row(
        "telegram_group", "Telegram — a group/room", "telegram receipt",
        "the send receipt (API 200 + message_id), confirmable by a room-ledger "
        "read-back",
        "`room_read(room=<chat_id>)` — the harness appends every allowlisted-room "
        "line to the local ledger",
        "the ledger holds what OTHERS wrote and what the harness captured; a "
        "missing line is a capture gap or the retention window, never on its own "
        "proof that the send failed",
    ),
    "email": _row(
        "email", "Email", "smtp 250 + Message-ID",
        "the SMTP 250 (or provider accept) plus the Message-ID stamped on the "
        "sent mail",
        "the send result carries the Message-ID; a reply arrives with it in "
        "In-Reply-To",
        "delivery to the server is not delivery to a human — a 250 proves the "
        "mail was accepted for delivery, never that it was read or that it "
        "escaped a spam filter",
    ),
    "onchain": _row(
        "onchain", "On-chain transaction", "tx hash",
        "a confirmed receipt AND the state read back afterwards",
        "the existing `tx_guard` rule: assert the simulated deltas, broadcast, "
        "then read the resulting state (balance, owner, tokenId) back",
        "a transaction hash is not a result: a receipt can succeed while the "
        "call did nothing. Only the read-back proves the effect",
    ),
}


def _facts_text(facts: Dict[str, Any]) -> str:
    parts = [f"{k}={v}" for k, v in facts.items()
             if v is not None and str(v).strip() != ""]
    return " " + " ".join(parts) if parts else ""


def verification_line(rail: str, **facts: Any) -> str:
    """The one-line proof note for ``rail``, carrying whatever receipt facts the
    caller actually has.

        >>> verification_line("telegram_channel", message_id=123)
        'proof: telegram receipt message_id=123 — the bot cannot read its own …'

    Returns ``""`` for a rail with no row — a caller appending this to a tool
    result must never fail because the table does not know its rail, and an
    invented proof rule would be worse than none. ``tests/unit/core/rails/``
    pins that every call site's key IS in the table.
    """
    row = VERIFICATION.get(str(rail or ""))
    if row is None:
        return ""
    return f"proof: {row.receipt}{_facts_text(facts)} — {row.note}"


def rail_for_message(surface: str, is_room: bool = False,
                     chat_type: Optional[str] = None) -> Optional[str]:
    """Which rail row a `message`-tool send lands on.

    Telegram splits: a CHANNEL has no read-back at all, a GROUP has the room
    ledger. When the caller cannot tell the two apart we return the CHANNEL row,
    because its note is the one that prevents the wrong conclusion (assuming a
    read-back exists is the failure; assuming it does not is merely cautious).
    """
    s = (surface or "").strip().lower()
    if s == "email":
        return "email"
    if s == "telegram":
        if (chat_type or "").strip().lower() in ("group", "supergroup"):
            return "telegram_group"
        return "telegram_group" if is_room else "telegram_channel"
    return None


def render_guide() -> str:
    """Render :data:`VERIFICATION` as `docs/guide/rails-verification.md`.

    The doc is GENERATED from the table so the two cannot drift; a test asserts
    the file on disk equals this output.
    """
    out = [
        "# Proof per rail",
        "",
        "<!-- GENERATED from core/rails/verification.py — do not edit by hand.",
        "     Regenerate: python -c \"import pathlib, core.rails.verification as v;"
        " pathlib.Path('docs/guide/rails-verification.md').write_text(v.render_guide())\" -->",
        "",
        "\"Did that land?\" has a different answer on every rail, and getting it",
        "wrong costs a turn in both directions. An agent that re-reads a Telegram",
        "channel to confirm its own post will never find it — Telegram does not",
        "deliver a bot's own channel messages back — and will wrongly report a",
        "delivered post as unconfirmed. An agent that treats a transaction hash as",
        "a result will wrongly report a no-op as done.",
        "",
        "This page is generated from the one table in the code",
        "(`core/rails/verification.py`), which is also what every tool result's",
        "`proof:` line is rendered from. There is no second copy to disagree with.",
        "",
        "**What this table states is what CAN be proved, never that anything WAS.**",
        "A rail whose proof is the receipt still needs the receipt.",
        "",
    ]
    for row in VERIFICATION.values():
        out += [
            f"## {row.title} (`{row.rail}`)",
            "",
            f"- **Proof:** {row.proof}",
            f"- **How to check:** {row.method}",
            f"- **Watch out:** {row.note}",
            "",
        ]
    out += [
        "## When you cannot prove it",
        "",
        "Say so precisely. `OUTCOME: BLOCKED — <exactly what you need>` is a real",
        "result; \"unconfirmed\" over a rail whose receipt IS the proof is not.",
        "",
    ]
    return "\n".join(out)


__all__ = ["GUIDE_PATH", "RailVerification", "VERIFICATION", "rail_for_message",
           "render_guide", "verification_line"]
