"""The owner's view of a money verb's report (2026-11-18: "very bloated").

A rail report is written for three readers at once: the guard's own audit
words (``guard:``, ``lane:``, ``gas:``), the agent (``RESULT:``, ``tx:``,
``block:`` — ``card_groups.receipt_facts``) and the seat's parsers (the
``value sent:`` line, the confirm line). The STORED text keeps every line:
the card row, the owner thread and ``/card_<id>`` are unchanged. This module
is only what a person reads on a card and in the result message:

* a line that says nothing went wrong is dropped (``guard: authorized``, an
  allowed lane, the gas limit, the dry-run sentence, the symbol note, a route
  check that AGREES, a passed bridge phase, the raw simulation deltas);
* a padded amount loses its zeros (``312.45000000`` → ``312.45``);
* the two value lines become one (``value: $25.00 + network fee $0.03``);
* the caps and the bound keep their numbers and lose their sentences.

⚠️ Never drops a line it does not know. A report with ANY refusal, failure or
warning word (:func:`passed`) comes back whole, word for word; in a
passed one an unknown guard reason, a held lane and every ``⚠️`` line stay.
"""
from __future__ import annotations

import re
from typing import List, Optional

from core.wallet.tx_guard_steps.value import SENT_VALUE_LABEL

#: An allowed lane says nothing the owner must act on; a held or refused one does.
_QUIET_LANES = frozenset({"autonomous", "owner_direct", "owner_approved"})
_MONEY = r"\$([0-9][0-9,]*\.?[0-9]*)"
_SIMULATED = re.compile(r"^\s*simulated value:\s*" + _MONEY + r"\s*$")
_SENT = re.compile(r"^\s*" + re.escape(SENT_VALUE_LABEL) + r"\s*" + _MONEY)
_CAPS = re.compile(r"^(\s*)caps:\s+per transaction \$(\S+); daily \$\S+ used of \$(\S+) "
                   r"\(\$(\S+) left\)\s*$")
_BOUND = re.compile(r"^(\s*)bound: the (?:send|swap) may be worth at most (\$[0-9][0-9,.]*)"
                    r"(?: \(([^:)]*)[:)].*)?$")


def _usd(text: str) -> Optional[float]:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _fee(usd: float) -> str:
    usd = round(usd, 4)                 # 5.01 - 5.00 is 0.00999… in floats
    return "<$0.01" if 0 < usd < 0.01 else f"${usd:,.2f}"


def _value(usd: float) -> str:
    return f"${usd:,.2f}" if usd >= 1 or usd == 0 else f"${usd:.4f}"


#: A line that starts with one of these says a check PASSED (or names an
#: internal id): nothing for the owner to act on.
_QUIET_STARTS = (
    "gas:", "RESULT: DRY RUN", "[DRY RUN]", "note: quoted token symbols",
    "route check: AGREES", "phase 1: assertions passed", "request id:",
    "independent value check: arrival floor and impact within bounds",
    "origin  : confirmed on-chain", "simulated: ",
    "phase 2 : would assert the measured arrival",
)
#: Words beyond the card's own failure and refusal marks (``cards.failed_result``,
#: ``cards.refused_quote``) that mean the report did NOT simply pass.
_NOT_PASSED = ("REFUSED", "DISAGREES", "NOT VERIFIED")
_REPORT_LINE = re.compile(r"^  (?:guard|RESULT|lane):", re.MULTILINE)
_CHAIN_ID = re.compile(r" \(chain \d+\)")
_FLOOR = re.compile(r"^(\s*)ARRIVAL FLOOR: (.+?) — phase 2 asserts.*$")
_PHASE2 = re.compile(r"^(\s*)phase 2 : ")
_LONG_DECIMAL = re.compile(r"(?<![\w.])(\d+\.\d{6,})(?![\w.])")


def _trim(m: "re.Match") -> str:
    whole, _, frac = m.group(1).partition(".")
    frac = frac.rstrip("0")
    return f"{whole}.{frac.ljust(2, '0')}"


def _quiet(line: str) -> bool:
    s = line.strip()
    if s == "guard: authorized" or s.startswith(_QUIET_STARTS):
        return True
    return s.startswith("lane:") and (s[5:].split() or [""])[0] in _QUIET_LANES


def is_report(text: str) -> bool:
    """A rail report: it carries the guard's verdict, lane or result line."""
    return bool(text) and bool(_REPORT_LINE.search(text))


def passed(text: str) -> bool:
    """True for a report with no refusal, failure or warning word in it."""
    from core.surfaces import cards
    return (is_report(text) and not cards.failed_result(text)
            and not cards.refused_quote(text) and not any(w in text for w in _NOT_PASSED))


def owner_view(text: str) -> str:
    """``text`` as a person reads it; anything that is not a rail report, and
    a report that did not simply pass, comes back unchanged."""
    if not passed(text):
        return text
    lines = text.splitlines()
    out: List[str] = []
    for i, line in enumerate(lines):
        if _quiet(line):
            continue
        sim = _SIMULATED.match(line)
        nxt = _SENT.match(lines[i + 1]) if sim and i + 1 < len(lines) else None
        if nxt:
            continue                      # folded into the value line below
        sent = _SENT.match(line)
        if sent:
            prev = _SIMULATED.match(lines[i - 1]) if i else None
            value, total = _usd(sent.group(1)), _usd(prev.group(1)) if prev else None
            if value is not None:
                fee = "" if total is None or total <= value else (
                    f" + network fee {_fee(total - value)}")
                out.append(f"  value: {_value(value)}{fee}")
                continue
        caps = _CAPS.match(line)
        if caps:
            out.append(f"{caps.group(1)}limits: ${caps.group(2)} per transaction · "
                       f"${caps.group(4)} of ${caps.group(3)} left today")
            continue
        bound = _BOUND.match(line)
        if bound:
            why = f" ({bound.group(3).strip()})" if bound.group(3) else ""
            out.append(f"{bound.group(1)}max: {bound.group(2)}{why}")
            continue
        floor = _FLOOR.match(line)
        if floor:
            line = f"{floor.group(1)}minimum arrival: {floor.group(2)}"
        line = _PHASE2.sub(r"\1", _CHAIN_ID.sub("", line))
        out.append(_LONG_DECIMAL.sub(_trim, line))
    return "\n".join(out).rstrip()
