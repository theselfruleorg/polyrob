"""Provenance stamping for the durable identity docs (057 WS-D / R3).

The S3 class: the agent writes a sentence like "X lacks permission to post"
into ``owner.md``, it is injected as a frozen foundation block every session
from then on, and NOTHING on the read side says where it came from or when it
was true. Two months later the agent is still steering on a July observation it
cannot date.

This module is the pure half of the fix: :func:`stamp_changed_lines` renders
every NEW or CHANGED line of a document with a trailing
``[from: <source> <YYYY-MM-DD>]`` when the writer passes a ``source``. Unchanged
lines keep the provenance they already carry — a rewrite of one paragraph must
not re-date the rest of the document. Nothing here refuses a write.

Pure ``core`` (stdlib only) so both document writers, the tool
layer and the tests share ONE implementation.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional


#: The trailing provenance stamp this module writes and recognises. Kept
#: deliberately loose on the source (free text, e.g. ``room_read``,
#: ``owner said``, ``measured``) and strict on the date shape.
def _stamp_parts(line: str):
    # Search once from the right; a regex retrying every '[from:' prefix with
    # overlapping whitespace quantifiers is cubic on malformed source text.
    tail = line.rstrip()
    if not tail.endswith("]"):
        return None
    start = tail.rfind("[from:")
    if start < 0:
        return None
    payload = tail[start + 6:-1].strip()
    if "]" in payload:
        return None
    parts = payload.rsplit(None, 1)
    if not parts or re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[-1]) is None:
        return None
    return start, parts[-1]


#: 060 WS-6: the heading that starts a rules doc's superseded section.
_SUPERSEDED_RE = re.compile(r"^\s*##\s+superseded\s*$", re.IGNORECASE)

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
    stamp = _stamp_parts(line)
    return line[:stamp[0]].rstrip() if stamp else line


def has_stamp(line: str) -> bool:
    """Does this line already carry a provenance stamp?"""
    return _stamp_parts(line) is not None


def stamp_of(line: str) -> Optional[str]:
    """The ``YYYY-MM-DD`` of this line's stamp, or ``None`` when undated."""
    stamp = _stamp_parts(line)
    return stamp[1] if stamp else None


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
SUPERSEDED_MAX_CHARS = 48000  # 2026-10-11: 2x OWNER_DOC_MAX_CHARS (24000)

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


def split_superseded_raw(text: str):
    """``(head, tail)`` with ``head + tail == text`` exactly: ``tail`` starts at the
    ``## Superseded`` heading line (``""`` when there is none). A patch edits
    ``head`` only and re-joins the untouched ``tail`` — history is never rewritten
    by a string replace (prod 2026-10-09/11: a ``replace_all`` rewrote retired
    entries and the active rule kept the old cron id)."""
    text = text or ""
    pos = 0
    for line in text.splitlines(keepends=True):
        if _SUPERSEDED_RE.match(line.rstrip("\r\n")):
            return text[:pos], text[pos:]
        pos += len(line)
    return text, ""


_SUPERSEDED_DATE_RE = re.compile(r"(— superseded) \d{4}-\d{2}-\d{2}")


def _entry_key(line: str) -> str:
    """Dedup key of one superseded entry: the entry WITHOUT its retirement date.
    The same rule retired with the same successor on two days (a draft diffed
    once at queue time and again at promote time) is one fact, not two."""
    return _SUPERSEDED_DATE_RE.sub(r"\1", line.strip())


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
    # Exact AND date-only duplicates collapse (first kept) — prod 2026-10-11
    # held one retired rule 3x, differing only in "superseded <date>".
    for line in (old_sup + "\n" + new_sup).splitlines():
        if line.strip() and _entry_key(line) not in seen:
            seen.add(_entry_key(line))
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
            if _entry_key(entry) not in seen:
                seen.add(_entry_key(entry))
                entries.append(entry)
    if not entries:
        return new
    # Bound the section; the oldest fall off (archived with the prior doc).
    while entries and len("\n".join(entries)) > SUPERSEDED_MAX_CHARS:
        entries.pop(0)
    return new_active.rstrip() + "\n\n" + SUPERSEDED_HEADING + "\n\n" + "\n".join(entries) + "\n"


def _rule_keys(text: str) -> set:
    return {strip_stamp(l).strip() for l in active_rule_lines(text)}


def rebase_draft(base: str, draft: str, current: str):
    """Apply a pending draft's CHANGE to the CURRENT active doc (a three-way merge
    over active rule lines; ``base`` = the active doc the draft was drafted against).

    Promote used to activate the draft's whole body, so every live line the draft
    never saw (added after it was drafted) moved under ``## Superseded``, and a line
    retired since came back to life (prod 10-10 duplicate stale line, 10-15 ~60-line
    near miss). Per active line:

    - in the draft, not in ``base``: the draft added it -> kept;
    - in ``base`` and the draft, gone from ``current``: retired since -> dropped;
    - in ``base``, not in the draft: the draft retired it -> retired (named);
    - in ``current``, not in ``base`` nor the draft: added since -> kept (named).

    Returns ``(merged, retired, kept)``; ``retired``/``kept`` are the ``current``
    lines the promote retires / keeps although the draft lacks them.
    """
    base_k, draft_k, cur_k = _rule_keys(base), _rule_keys(draft), _rule_keys(current)
    head, tail = split_superseded_raw(draft)
    out, dropped = [], False
    for line in head.splitlines():
        k = strip_stamp(line).strip()
        is_rule = bool(line.strip()) and not line.lstrip().startswith("#")
        if is_rule and k in base_k and k not in cur_k:
            dropped = True
            continue  # the draft did not touch it, and it was retired since
        out.append(line)
    cur_lines = active_rule_lines(current)
    kept = [l for l in cur_lines
            if strip_stamp(l).strip() not in base_k | draft_k]
    retired = [l for l in cur_lines
               if strip_stamp(l).strip() in base_k - draft_k]
    if not dropped and not kept:
        return draft, retired, kept
    merged_head = "\n".join(out).rstrip()
    if kept:
        merged_head += "\n\n" + "\n\n".join(l.rstrip() for l in kept)
    merged = merged_head + "\n" + (("\n" + tail) if tail else "")
    return merged, retired, kept


__all__ = [
    "SUPERSEDED_HEADING",
    "rebase_draft",
    "SUPERSEDED_MAX_CHARS",
    "active_rule_lines",
    "carry_superseded",
    "split_superseded",
    "split_superseded_raw",
    "superseded_entries",
    "changed_lines",
    "has_stamp",
    "normalize_observed_at",
    "render_stamp",
    "stamp_changed_lines",
    "stamp_of",
    "strip_stamp",
    "today_iso",
]
