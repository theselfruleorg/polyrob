"""How old is what this document claims? (057 WS-D / R3, the age header.)

The owner-facts and evolving-SELF docs are pinned as a FROZEN foundation block
at session start, so every sentence in them reads with the same authority
whether it was measured this morning or asserted in July. `core.doc_claims`
puts a date on each line; this module turns those dates into the ONE header the
foundation block renders:

    ## Owner facts (last write 2026-09-19; 3 lines older than 30 days; 2 lines undated)

⚠️ An UNDATED line is reported as undated, never as fresh. Before
`DOC_CLAIM_PROVENANCE_REQUIRED` is armed every line is undated, so the header
says so instead of implying a document with no old claims in it.

Pure core (stdlib + ``core.doc_claims``). Never raises: an unparsable document
renders the plain heading, which is byte-identical to the pre-057 block.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from core.doc_claims import stamp_of, strip_stamp

#: A line older than this many days is called out in the header. 30 days is the
#: window past which "since" claims in the 09-19 prod docs were provably stale.
STALE_DAYS = 30


@dataclass(frozen=True)
class DocAge:
    """The age facts of one document."""

    total: int = 0            # non-blank content lines
    dated: int = 0            # lines carrying a [from: … <date>] stamp
    undated: int = 0          # lines with no stamp at all
    stale: int = 0            # dated lines older than STALE_DAYS
    newest: Optional[str] = None   # newest stamp date, YYYY-MM-DD
    oldest: Optional[str] = None   # oldest stamp date, YYYY-MM-DD


def _parse(value: Optional[str]) -> Optional[date]:
    """``YYYY-MM-DD`` -> ``date``, or None when it is not a real calendar date."""
    if not value:
        return None
    try:
        y, m, d = (int(p) for p in value.split("-"))
        return date(y, m, d)
    except Exception:
        return None


def _today(now: Optional[date] = None) -> date:
    return now or datetime.now(timezone.utc).date()


def measure_doc_age(text: str, *, now: Optional[date] = None,
                    stale_days: int = STALE_DAYS) -> DocAge:
    """Count the dated / undated / stale lines of ``text``. Never raises."""
    today = _today(now)
    total = dated = undated = stale = 0
    newest = oldest = None
    try:
        lines = (text or "").splitlines()
    except Exception:
        return DocAge()
    for line in lines:
        if not strip_stamp(line).strip():
            continue
        total += 1
        d = stamp_of(line)
        parsed = _parse(d)
        if parsed is None:
            # No stamp, or a stamp whose date is not a real date (only reachable
            # by a hand edit — the writer normalises). Either way we cannot say
            # when this line was true, so it counts as UNDATED, never as fresh.
            undated += 1
            continue
        dated += 1
        if newest is None or d > newest:
            newest = d
        if oldest is None or d < oldest:
            oldest = d
        if (today - parsed).days > stale_days:
            stale += 1
    return DocAge(total=total, dated=dated, undated=undated, stale=stale,
                  newest=newest, oldest=oldest)


def render_age_header(title: str, text: str, *, now: Optional[date] = None,
                      stale_days: int = STALE_DAYS) -> str:
    """The ``## <title> (…)`` heading for a provenance-stamped document.

    With no stamps at all the parenthetical names the undated count rather than
    claiming freshness; with no content lines it degrades to the bare ``## title``
    (byte-identical to the pre-057 heading).
    """
    age = measure_doc_age(text, now=now, stale_days=stale_days)
    if age.total == 0:
        return f"## {title}"
    parts = []
    if age.newest:
        parts.append(f"last write {age.newest}")
    if age.stale:
        parts.append(f"{age.stale} line(s) older than {stale_days} days")
    if age.undated:
        parts.append(f"{age.undated} line(s) undated")
    if not parts:
        return f"## {title}"
    return f"## {title} ({'; '.join(parts)})"


__all__ = ["DocAge", "STALE_DAYS", "measure_doc_age", "render_age_header"]
