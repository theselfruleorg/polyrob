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

#: 060 WS-6: the heading that starts a rules doc's superseded section.
_SUPERSEDED_RE = re.compile(r"^\s*##\s+superseded\s*$", re.IGNORECASE)

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
        if _SUPERSEDED_RE.match(line):
            break  # 060 WS-6: the superseded section is history, never a change
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
    lines = (new or "").splitlines()
    for idx, line in enumerate(lines):
        if _SUPERSEDED_RE.match(line):
            # 060 WS-6: the superseded section keeps the stamps it was retired
            # with; re-stamping it would date history as observed today.
            out.extend(lines[idx:])
            break
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


# --- 060 WS-6: supersede, never evict ---------------------------------------
#
# OpenClaw's USER.md model: a directive is ACTIVE or SUPERSEDED, and a superseded
# one is kept, dated, with its successor. Before this, a rule that was rewritten
# or deleted to make room under the owner-doc cap was simply gone — on 2026-09-20
# one rule fit only after two older enforcement anchors were cut. Now a line an
# update DROPS moves under ``## Superseded`` with the date (and its successor when
# the edit replaced exactly one line), the loader injects only the ACTIVE part,
# and the cap counts only the active part.

#: The ONE heading that starts the superseded section of a rules doc.
SUPERSEDED_HEADING = "## Superseded"

#: The superseded section's own bound. Past it the OLDEST entries fall off the
#: live file — nothing is lost: every active write archives the whole prior doc
#: first (``SelfContextWriter._archive_existing``). Kept at 2x OWNER_DOC_MAX_CHARS
#: so one whole retired doc still fits; the section is never injected.
SUPERSEDED_MAX_CHARS = 16000

def owner_rules_supersede() -> bool:
    """``OWNER_RULES_SUPERSEDE`` — supersede, never evict. Default ON.

    ``false`` restores the flat owner doc: a dropped line is gone, the loader
    injects the whole file and the cap counts the whole file. Literal name on
    purpose (``test_flags_reverse`` scans for it).
    """
    return bool_env("OWNER_RULES_SUPERSEDE", True)


def split_superseded(text: str):
    """``(active, superseded)`` — the doc before the ``## Superseded`` heading,
    and the section body after it (heading excluded). No heading => ``(text, "")``."""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if _SUPERSEDED_RE.match(line):
            active = "\n".join(lines[:i]).rstrip()
            sup = "\n".join(lines[i + 1:]).strip("\n")
            return active, sup
    return (text or ""), ""


def superseded_entries(text: str) -> List[str]:
    """The superseded section's entries, one per non-blank line, oldest first."""
    _active, sup = split_superseded(text)
    return [l for l in sup.splitlines() if l.strip()]


def active_rule_lines(text: str) -> List[str]:
    """The ACTIVE part's content lines (headings and blanks excluded)."""
    active, _sup = split_superseded(text)
    return [l for l in active.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def _entry_body(line: str) -> str:
    body = line.strip()
    if body.startswith("- "):
        body = body[2:]
    return body


def carry_superseded(old: str, new: str, *, observed_at: Optional[str] = None) -> str:
    """Return ``new`` with every ACTIVE rule line of ``old`` that ``new`` drops
    kept under ``## Superseded``, dated — never silently evicted.

    - The old doc's superseded entries are ALWAYS carried (a writer cannot erase
      the section by omitting it); entries ``new`` itself lists are kept too.
    - A dropped line gets ``— superseded <date>``; when the edit replaced exactly
      one line with exactly one line, ``by: <successor>`` names the new one.
    - Headings are structure, not rules, and are never carried.
    - Idempotent: nothing dropped and no section on either side => ``new`` as is.
    """
    new_active, new_sup = split_superseded(new)
    old_active, old_sup = split_superseded(old)
    new_bodies = {strip_stamp(l).strip() for l in new_active.splitlines()}
    dropped = [l for l in old_active.splitlines()
               if l.strip() and not l.lstrip().startswith("#")
               and strip_stamp(l).strip() not in new_bodies]
    entries: List[str] = []
    seen = set()
    for line in (old_sup + "\n" + new_sup).splitlines():
        if line.strip() and line.strip() not in seen:
            seen.add(line.strip())
            entries.append(line.rstrip())
    if dropped:
        date = normalize_observed_at(observed_at)
        added = changed_lines(old_active, new_active)
        successor = ""
        if len(dropped) == 1 and len(added) == 1:
            successor = _entry_body(strip_stamp(added[0])).strip()
            if len(successor) > 160:
                successor = successor[:159] + "…"
        for line in dropped:
            entry = f"- {_entry_body(line)} — superseded {date}"
            if successor:
                entry += f" by: {successor}"
            if entry.strip() not in seen:
                seen.add(entry.strip())
                entries.append(entry)
    if not entries:
        return new
    # Bound the section; the oldest fall off (archived with the prior doc).
    while entries and len("\n".join(entries)) > SUPERSEDED_MAX_CHARS:
        entries.pop(0)
    return new_active.rstrip() + "\n\n" + SUPERSEDED_HEADING + "\n\n" + "\n".join(entries) + "\n"


__all__ = [
    "SUPERSEDED_HEADING",
    "SUPERSEDED_MAX_CHARS",
    "active_rule_lines",
    "carry_superseded",
    "owner_rules_supersede",
    "split_superseded",
    "superseded_entries",
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
