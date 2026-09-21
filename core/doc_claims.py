"""Provenance stamping + the claim-format guard for the durable identity docs
(057 WS-D / R3).

The S3 class: the agent writes a sentence like "X lacks permission to post"
into ``owner.md``, it is injected as a frozen foundation block every session
from then on, and NOTHING on the read side says where it came from or when it
was true. Two months later the agent is still steering on a July observation it
cannot date.

This module is the pure half of the fix. It does two things and neither of them
is a truth check:

1. :func:`stamp_changed_lines` renders every NEW or CHANGED line of a document
   with a trailing ``[from: <source> <YYYY-MM-DD>]``. Unchanged lines keep the
   provenance they already carry — a rewrite of one paragraph must not re-date
   the rest of the document.
2. :func:`find_unsourced_claims` returns the new/changed lines that READ like a
   durable claim about the world (the claim lexicon) when the caller supplied no
   ``source``. The writer turns that into a refusal naming the line and the
   remedy.

⚠️ **A FORMAT check, not a truth check.** Nothing here knows whether a claim is
true. It makes "the owner told me in July" distinguishable from "I measured this
today", which is the whole of the S3 class; a false sentence with a source is
still a false sentence, and it will still land.

Pure ``core`` (stdlib + ``core.env`` only) so both document writers, the tool
layer and the tests share ONE implementation.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

from core.env import bool_env

#: The trailing provenance stamp this module writes and recognises. Kept
#: deliberately loose on the source (free text, e.g. ``room_read``,
#: ``owner said``, ``measured``) and strict on the date shape.
_STAMP_RE = re.compile(r"\s*\[from:\s*(?P<source>[^\]]*?)\s+(?P<date>\d{4}-\d{2}-\d{2})\s*\]\s*$")

#: 057 §1 R3's lexicon. A line matching any of these is making a durable claim
#: about what is true or permitted — exactly the shape that must not be datable
#: only by guesswork. Word-ish boundaries so "sincere" is not "since".
_CLAIM_TERMS = (
    r"lacks",
    r"needs",
    r"is not allowed",
    r"permission",
    r"unconfirmed",
    r"cannot post",
    r"can'?t post",
    r"disabled",
    r"broken",
    r"since",
    r"no longer",
    r"does not work",
    r"doesn'?t work",
    r"blocked",
)
_CLAIM_RE = re.compile(r"\b(?:" + "|".join(_CLAIM_TERMS) + r")\b", re.IGNORECASE)

def claim_provenance_required() -> bool:
    """Is the claim guard armed? ``DOC_CLAIM_PROVENANCE_REQUIRED``, default OFF.

    Default OFF keeps every existing write byte-identical; prod arms it once the
    docs have been re-sourced. Read at call time (never frozen at import) so an
    operator flip needs no redeploy of the writer.

    ⚠️ The name is written as a LITERAL here on purpose. ``test_flags_reverse``
    scans the tree for ``bool_env(<literal name>)`` to prove every env read is
    catalogued; a name hidden behind a module constant is invisible to it, which
    is the exact class that contract exists to catch.
    """
    return bool_env("DOC_CLAIM_PROVENANCE_REQUIRED", False)


def today_iso() -> str:
    """Today's date in UTC, ``YYYY-MM-DD``. The default for ``observed_at``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def normalize_observed_at(value: Optional[str]) -> str:
    """Return a ``YYYY-MM-DD`` date, defaulting to today.

    An unparsable/odd value falls back to today rather than raising: the stamp is
    metadata on an otherwise valid write, and refusing a document because its
    date string had a stray space would be a worse failure than dating it now.
    """
    raw = (value or "").strip()
    if not raw:
        return today_iso()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if not m:
        return today_iso()
    try:
        datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return today_iso()
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def strip_stamp(line: str) -> str:
    """The line without its trailing provenance stamp (if any)."""
    return _STAMP_RE.sub("", line)


def has_stamp(line: str) -> bool:
    """Does this line already carry a provenance stamp?"""
    return _STAMP_RE.search(line) is not None


def stamp_of(line: str) -> Optional[str]:
    """The ``YYYY-MM-DD`` of this line's stamp, or ``None`` when undated."""
    m = _STAMP_RE.search(line)
    return m.group("date") if m else None


def render_stamp(source: str, observed_at: Optional[str] = None) -> str:
    """The trailing stamp text for ``source`` observed on ``observed_at``."""
    src = " ".join(str(source or "").split()) or "unstated"
    # A ']' inside the source would break the stamp's own grammar on re-read.
    src = src.replace("]", ")").replace("[", "(")
    return f" [from: {src} {normalize_observed_at(observed_at)}]"


def changed_lines(old: str, new: str) -> List[str]:
    """The NEW/CHANGED content lines of ``new`` relative to ``old``.

    Comparison ignores each line's provenance stamp, so re-stamping a line does
    not make it look changed. Blank lines are never "changed".
    """
    old_bodies = {strip_stamp(l).strip() for l in (old or "").splitlines()}
    out = []
    for line in (new or "").splitlines():
        body = strip_stamp(line).strip()
        if not body:
            continue
        if body not in old_bodies:
            out.append(body)
    return out


def stamp_changed_lines(old: str, new: str, source: str,
                        observed_at: Optional[str] = None) -> str:
    """Return ``new`` with every new/changed line carrying a provenance stamp.

    Unchanged lines are returned as the author wrote them, EXCEPT that a line
    whose content already existed and carried a stamp keeps that older stamp
    when the author dropped it — provenance sticks to the sentence, so a
    full-document ``update`` that re-types an old line does not silently
    re-date it as observed today.

    Idempotent: stamping an already-stamped unchanged document is a no-op.
    """
    old_lines = {}
    for l in (old or "").splitlines():
        body = strip_stamp(l).strip()
        if body and body not in old_lines:
            old_lines[body] = l
    out = []
    for line in (new or "").splitlines():
        body = strip_stamp(line).strip()
        if not body:
            out.append(line)
            continue
        if body in old_lines:
            out.append(line if has_stamp(line) else old_lines[body])
            continue
        # A NEW line the author already stamped keeps that stamp: provenance
        # sticks to the sentence, and a promotion must not re-date or
        # re-attribute what the draft said about where a claim came from.
        out.append(line if has_stamp(line)
                   else strip_stamp(line).rstrip() + render_stamp(source, observed_at))
    text = "\n".join(out)
    if (new or "").endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text


def find_unsourced_claims(old: str, new: str, source: Optional[str]) -> List[str]:
    """New/changed lines that make a durable CLAIM with no ``source`` given.

    ⚠️ This is a **format** check, not a truth check: it asks "can a reader tell
    where this came from", never "is this true". A sourced falsehood passes.

    Returns ``[]`` when a non-blank ``source`` was supplied (the format question
    is answered) or when no new/changed line matches the claim lexicon.
    """
    if (source or "").strip():
        return []
    # A changed line that already carries its own stamp HAS answered the format
    # question — the author sourced it when they wrote it. Prod 2026-09-20: the
    # owner's promotion of a fully-stamped draft was refused here because the
    # diff against the active doc made every line "changed" and this never
    # looked at the stamp. ``changed_lines`` strips stamps for the comparison,
    # so re-derive against the original lines.
    stamped = {strip_stamp(l).strip() for l in (new or "").splitlines() if has_stamp(l)}
    return [line for line in changed_lines(old, new)
            if _CLAIM_RE.search(line) and line not in stamped]


def unsourced_claim_error(lines: List[str]) -> str:
    """The ONE refusal sentence for a claim guard trip — names the line AND the
    remedy, so the agent can retry without guessing what the writer wanted."""
    shown = lines[0] if lines else ""
    if len(shown) > 160:
        shown = shown[:159] + "…"
    more = f" (and {len(lines) - 1} more line(s))" if len(lines) > 1 else ""
    return (
        f"this line makes a durable claim with no provenance: {shown!r}{more} — "
        f"add source= (e.g. source='room_read', 'owner said', 'measured') and "
        f"observed_at=YYYY-MM-DD (defaults to today) and retry. "
        f"This is a FORMAT check: it asks where the claim came from, not whether "
        f"it is true."
    )


__all__ = [
    "claim_provenance_required",
    "changed_lines",
    "find_unsourced_claims",
    "has_stamp",
    "normalize_observed_at",
    "render_stamp",
    "stamp_changed_lines",
    "stamp_of",
    "strip_stamp",
    "today_iso",
    "unsourced_claim_error",
]
