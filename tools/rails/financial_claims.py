"""Verify before claiming financial success (proposal 022) — the pure half.

On 2026-07-19 the agent posted "First x402 micro-transaction completed!" as a
public thread and sent its owner the same sentence. No payment existed: its own
`x402_wallet_status`, read twelve seconds earlier, said "0 x402 payments". The
completion judge caught it afterwards; a post-hoc check cannot un-send a tweet.

So the gate sits on the SEND. This module is the part that needs no I/O:

- :func:`detect_claim` — does this outbound text assert that money MOVED
  (paid / earned / received / settled + an amount or a money noun)? A report
  that says the payment did NOT happen, failed, is pending, or is a plan is
  never a claim. False positives on honest failure reports are the main risk,
  so every sentence with a negation / hedge / future cue is skipped.
- :func:`verify_claim` — given the settled records (the wallet audit log for
  money OUT, settled invoices for money IN), is there a record that matches?
  Returns ``None`` (allowed) or the named refusal text the agent can act on.

The tools-tier hook (`tools/controller/financial_claim_gate.py`) reads the
records and calls these two. Invoice-COUNT exaggeration ("9 pending invoices")
is deliberately out of scope (owner decision, 2026-09-23).

Pure core: stdlib only.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

#: The refusal slug. It prefixes every refusal so the agent (and a log grep)
#: can tell this gate from every other one.
REFUSAL_SLUG = "financial_claim_unverified"

#: A claim that names a USD amount must match a record of that amount inside
#: this window. A claim with no USD amount must be backed by a record inside
#: the short window — "just completed my first payment" is a claim about now.
AMOUNT_LOOKBACK_SEC = 30 * 24 * 3600
FRESH_LOOKBACK_SEC = 24 * 3600

#: Relative tolerance for an amount match (rounding in prose: "$1" for 0.998).
_AMOUNT_TOLERANCE = 0.05
_AMOUNT_FLOOR_USD = 0.01

SPEND = "spend"
RECEIVE = "receive"
ANY = "any"

_I = re.IGNORECASE

# Success verbs, split by direction. Keep these short and specific.
_SPEND_RE = re.compile(
    r"\b(?:"
    r"(?:i|we|rob|agent|it)\s+(?:just\s+|have\s+|has\s+|successfully\s+)*"
    r"(?:paid|spent|transferred)"
    r"|successfully\s+(?:paid|sent|transferred|purchased)"
    r"|(?:executed|made|completed)\s+(?:my|our|the|a|an|its)?\s*(?:first\s+)?"
    r"(?:autonomous\s+)?(?:agent\s+)?(?:x402\s+)?"
    r"(?:payment|transaction|micro-?transaction|purchase|transfer)"
    r"|payment\s+(?:was\s+|has\s+been\s+)?(?:completed|made|sent|succeeded|"
    r"confirmed|went\s+through)"
    r")\b", _I)

_RECEIVE_RE = re.compile(
    r"\b(?:"
    r"earned|got\s+paid|(?:was|were|been|am|are|is)\s+paid|received\s+(?:a\s+)?"
    r"(?:payment|\$|\d|usdc|usd|revenue|funds|money|income)"
    r"|(?:made|generated|collected)\s+(?:my\s+|our\s+|its\s+)?(?:first\s+)?"
    r"(?:\$|revenue|income|sale|money|\d)"
    r"|first\s+(?:revenue|sale|income|paying\s+customer)"
    r"|invoice\s+(?:was\s+|has\s+been\s+)?(?:paid|settled)"
    r")", _I)

_EITHER_RE = re.compile(
    r"\b(?:"
    r"(?:transaction|micro-?transaction|transfer|purchase|swap|settlement)\s+"
    r"(?:was\s+|has\s+been\s+|is\s+)?(?:completed|complete|successful|succeeded|"
    r"confirmed|executed|settled)"
    r"|settled"
    r")\b", _I)

# "sent"/"bought" are everyday verbs ("I sent you the invoice"): they are a
# money claim only with an AMOUNT in the same sentence.
_SPEND_NEEDS_AMOUNT_RE = re.compile(
    r"\b(?:i|we|rob|agent|it)\s+(?:just\s+|have\s+|has\s+)*"
    r"(?:sent|purchased|bought)\b", _I)

# Money nouns: without an amount, a success verb needs one of these in the same
# sentence to be a money claim ("received your email" is not).
_MONEY_NOUN_RE = re.compile(
    r"\b(?:payment|paid|transaction|micro-?transaction|x402|invoice|revenue|"
    r"income|usdc|usdt|usd|dai|eth|sol|btc|wei|funds|money|dollars?|cents?|"
    r"sale)\b|\$", _I)

# Amounts. USD-denominated ones are comparable; the others only prove a claim.
_USD_AMOUNT_RE = re.compile(
    r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m)?\b"
    r"|(\d[\d,]*(?:\.\d+)?)\s*(usd|usdc|usdt|dai|dollars?|cents?)\b", _I)
_OTHER_AMOUNT_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(eth|sol|btc|wei|gwei|lamports?)\b", _I)

# A sentence carrying any of these is a report of an attempt, a failure, a
# plan or a question — never a success claim. False negatives here are cheap;
# false positives block an honest report, which is the harm to avoid.
_HEDGE_RE = re.compile(
    r"\b(?:not|no|never|none|nothing|cannot|can't|couldn't|didn't|did\s+not|"
    r"wasn't|weren't|isn't|aren't|haven't|hasn't|won't|failed|fail|fails|"
    r"failure|unable|attempt|attempted|attempting|tried|trying|try|pending|"
    r"unconfirmed|unverified|awaiting|waiting|expect|expected|hope|hoping|"
    r"plan|planned|planning|will|would|could|should|might|may|going\s+to|"
    r"intend|if|once|when|until|unless|unpaid|refund|refunded|simulated|"
    r"simulation|dry[\s-]?run|testnet|hypothetical|goal|target|aim|want|"
    r"invoice\s+(?:created|sent|issued)|to\s+be\s+paid|zero)\b"
    r"|n't\b|\?", _I)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\n])\s+|\n+")

# Reported quotations are evidence about somebody else's words, not an assertion
# that this agent moved money. Bare quotes and self-assertions remain gated.
_REPORTED_QUOTE_RE = re.compile(
    r"\b(?:the\s+)?(?:vendor|owner|customer|documentation(?:\s+example)?|"
    r"error(?:\s+message)?|example(?:\s+response)?|log(?:\s+entry)?)\s+"
    r"(?:says?|said|reads?|read|wrote|reports?|reported|is)\s*:?\s*"
    r'''(?:"[^"]*"|'[^']*'|“[^”]*”|‘[^’]*’)''', _I)

# Remove only the nonfinancial verb phrase, never skip the whole sentence: a
# later genuine payment claim in the same report still needs a settled record.
_NON_MONEY_ACTION_RE = re.compile(
    r"\b(?:paid\s+(?:(?:close|careful|special)\s+)?(?:attention|tribute|respects?)"
    r"|spent\s+(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"several|a\s+few)\s+(?:hours?|minutes?|seconds?|days?|weeks?)"
    r"|sent\s+(?:(?:you|them|him|her)\s+)?(?:(?:a|an|the|your)\s+)?"
    r"(?:invoice|quote|email|message|report|receipt|proposal))\b", _I)


@dataclass(frozen=True)
class FinancialClaim:
    """One outbound sentence that asserts money moved."""

    sentence: str
    direction: str                      # spend | receive | any
    usd_amounts: tuple = ()             # comparable amounts, in USD
    has_other_amount: bool = False      # e.g. "0.5 ETH" — not comparable


@dataclass
class SettledRecord:
    """One settled money movement, as the gate sees it."""

    direction: str                      # spend | receive
    amount_usd: float
    ts: float
    ref: str = ""
    source: str = ""                    # "wallet audit" | "settled invoices"


@dataclass
class RecordsRead:
    """What the hook could read. ``unreadable`` names every store that could not
    be read — an unreadable store is NOT an empty one, and the refusal says so."""

    records: List[SettledRecord] = field(default_factory=list)
    unreadable: List[str] = field(default_factory=list)


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _usd_amounts(sentence: str) -> tuple:
    out = []
    for m in _USD_AMOUNT_RE.finditer(sentence):
        if m.group(1) is not None:
            v = _to_float(m.group(1))
            mult = {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1.0)
            if v is not None:
                out.append(v * mult)
        else:
            v = _to_float(m.group(3))
            unit = (m.group(4) or "").lower()
            if v is not None:
                out.append(v / 100.0 if unit.startswith("cent") else v)
    return tuple(out)


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s and s.strip()]


def detect_claim(text: str) -> Optional[FinancialClaim]:
    """The first sentence in ``text`` that claims money moved, or ``None``."""
    claims = detect_claims(text, limit=1)
    return claims[0] if claims else None


def detect_claims(text: str, *, limit: int = 20) -> List[FinancialClaim]:
    """Every sentence in ``text`` that claims money moved (at most ``limit``).

    A claim needs a success verb AND (an amount OR a money noun) in the SAME
    sentence, and no hedge / negation / future cue in that sentence. An amount
    of zero is not a claim ("we earned $0 so far" is an honest report).
    """
    found: List[FinancialClaim] = []
    for sentence in _sentences(text):
        if len(found) >= limit:
            break
        original = sentence
        sentence = _NON_MONEY_ACTION_RE.sub("", _REPORTED_QUOTE_RE.sub("", sentence))
        if _HEDGE_RE.search(sentence):
            continue
        needs_amount = False
        if _SPEND_RE.search(sentence):
            direction = SPEND
        elif _SPEND_NEEDS_AMOUNT_RE.search(sentence):
            direction, needs_amount = SPEND, True
        elif _RECEIVE_RE.search(sentence):
            direction = RECEIVE
        elif _EITHER_RE.search(sentence):
            direction = ANY
        else:
            continue
        usd = _usd_amounts(sentence)
        other = bool(_OTHER_AMOUNT_RE.search(sentence))
        if usd and all(v <= 0 for v in usd) and not other:
            continue
        usd = tuple(v for v in usd if v > 0)
        if needs_amount and not (usd or other):
            continue
        if not (usd or other or _MONEY_NOUN_RE.search(sentence)):
            continue
        found.append(FinancialClaim(sentence=original[:240], direction=direction,
                                    usd_amounts=usd, has_other_amount=other))
    return found


def _amount_matches(claimed: float, recorded: float) -> bool:
    tol = max(_AMOUNT_FLOOR_USD, abs(claimed) * _AMOUNT_TOLERANCE)
    return abs(claimed - recorded) <= tol


def _direction_ok(claim_dir: str, record_dir: str) -> bool:
    return claim_dir == ANY or claim_dir == record_dir


def matching_record(claim: FinancialClaim, records: Iterable[SettledRecord],
                    *, now: Optional[float] = None) -> Optional[SettledRecord]:
    """The settled record that backs ``claim``, or ``None``.

    Every claimed USD amount must be matched by a record of the claimed
    direction inside :data:`AMOUNT_LOOKBACK_SEC`. A claim with no comparable
    amount needs any record of that direction inside :data:`FRESH_LOOKBACK_SEC`.
    """
    now = time.time() if now is None else float(now)
    pool = [r for r in records
            if _direction_ok(claim.direction, r.direction) and r.amount_usd > 0]
    if claim.usd_amounts:
        recent = [r for r in pool if now - float(r.ts or 0) <= AMOUNT_LOOKBACK_SEC]
        first = None
        for amount in claim.usd_amounts:
            hit = next((r for r in recent if _amount_matches(amount, r.amount_usd)), None)
            if hit is None:
                return None
            first = first or hit
        return first
    fresh = [r for r in pool if now - float(r.ts or 0) <= FRESH_LOOKBACK_SEC]
    return fresh[0] if fresh else None


def _direction_words(direction: str) -> str:
    return {SPEND: "a completed payment", RECEIVE: "money received",
            ANY: "a settled transaction"}.get(direction, "a settled transaction")


def _stores_for(direction: str) -> str:
    if direction == SPEND:
        return "the wallet audit log (x402_wallet_status)"
    if direction == RECEIVE:
        return "the settled invoices (x402_invoice_accounting)"
    return "the wallet audit log or the settled invoices"


def verify_claim(claim: Optional[FinancialClaim], read: RecordsRead,
                 *, now: Optional[float] = None) -> Optional[str]:
    """``None`` when the send may go; otherwise the named refusal.

    The refusal names the sentence, what is missing, which store to read, and
    the two ways forward (verify, or rephrase as an attempt). An unreadable
    store blocks too — "I could not read it" is not "it holds a payment" — and
    the refusal says which store it could not read.
    """
    if claim is None:
        return None
    hit = matching_record(claim, read.records, now=now)
    if hit is not None:
        return None
    amounts = ", ".join(f"${a:.2f}" for a in claim.usd_amounts)
    what = _direction_words(claim.direction) + (f" of {amounts}" if amounts else "")
    if read.unreadable:
        why = ("the gate could not read " + ", ".join(read.unreadable)
               + ", so it cannot confirm the claim")
    else:
        window = "30 days" if claim.usd_amounts else "24 hours"
        why = (f"{_stores_for(claim.direction)} shows no matching settled "
               f"record in the last {window}")
    return (f"refused ({REFUSAL_SLUG}): this message claims {what} — "
            f"\"{claim.sentence}\" — but {why}. Read {_stores_for(claim.direction)} "
            "first and send only what it shows, or rephrase as an attempt "
            "(\"I tried to pay …; it did not complete\"). Nothing was sent.")


def outbound_texts(params: object, fields: Sequence[str]) -> List[str]:
    """The text fields of an outbound call's params (dict or model), flattened —
    a thread's ``texts`` list contributes each post."""
    out: List[str] = []
    for name in fields:
        if isinstance(params, dict):
            value = params.get(name)
        else:
            value = getattr(params, name, None)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (list, tuple)):
            out.extend(v for v in value if isinstance(v, str))
    return out


__all__ = [
    "AMOUNT_LOOKBACK_SEC", "ANY", "FRESH_LOOKBACK_SEC", "FinancialClaim",
    "RECEIVE", "REFUSAL_SLUG", "RecordsRead", "SPEND", "SettledRecord",
    "detect_claim", "detect_claims", "matching_record", "outbound_texts", "verify_claim",
]
