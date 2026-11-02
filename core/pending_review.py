"""What the owner reads before deciding a pending proposal.

The notice used to show ``preview[:137] + "…"`` of a whole SKILL.md flattened
to one line — mostly its frontmatter, after a ``[drafted by …]`` note took its
own share of the 140 characters — and the chat seats had no way to read more.
An owner could not tell what a skill update CHANGED, only that one existed.

Two readers, one module:

* :func:`summary_line` — one honest line per item for the notice and the
  ``/pending`` list: new skill or update (lines added/removed), and the skill's
  own ``description`` rather than its first bytes.
* :func:`review_text` — the full review a tap opens (``/pending_p_<hex>``):
  an update as a diff against the live skill, a new item as its whole body.

Tier 0 (difflib, re); the skill manager is duck-typed (it lives
in ``agents``), so nothing here imports upward.
"""
from __future__ import annotations

import difflib
import re
from typing import Optional, Tuple

#: A review is one chat message thread, not a file dump. Over this the text is
#: cut with a note naming where the whole body lives.
REVIEW_MAX_CHARS = 12000
#: The notice / list line: the description, never the frontmatter.
SUMMARY_MAX_CHARS = 220

_FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def split_frontmatter(body: str) -> Tuple[dict, str]:
    """``({key: value}, rest)`` for a SKILL.md — simple ``key: value`` lines only."""
    text = body or ""
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not line.startswith((" ", "\t")):
            meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta, text[m.end():]


def _one_line(text: str, cap: int) -> str:
    out = " ".join(str(text or "").split())
    return out if len(out) <= cap else out[: cap - 1] + "…"


def skill_change(new_body: str, active_body: Optional[str]) -> dict:
    """``{"new", "added", "removed", "description"}`` for one skill draft."""
    meta, rest = split_frontmatter(new_body or "")
    description = meta.get("description") or _one_line(rest, SUMMARY_MAX_CHARS)
    if active_body is None:
        return {"new": True, "added": len((new_body or "").splitlines()),
                "removed": 0, "description": description}
    added = removed = 0
    for line in difflib.unified_diff((active_body or "").splitlines(),
                                     (new_body or "").splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"new": False, "added": added, "removed": removed,
            "description": description}


def summary_line(item: dict) -> str:
    """The text after the item's label on a notice or list line.

    The author/link note (``item["notes"]``) is kept whole and the cap applies
    to the CONTENT only — the note used to eat the space the content needed."""
    notes = item.get("notes") or ""
    change = item.get("change")
    if isinstance(change, dict):
        what = ("new skill" if change.get("new")
                else f"update: +{change.get('added', 0)} / −{change.get('removed', 0)} lines")
        body = f"{what} — {_one_line(change.get('description', ''), SUMMARY_MAX_CHARS)}"
    else:
        raw = str(item.get("preview") or "")
        if notes and raw.startswith(f"[{notes}]"):
            raw = raw[len(notes) + 2:]
        body = _one_line(raw, SUMMARY_MAX_CHARS)
    return (f"[{notes}] {body}" if notes else body).strip()


def _cap(text: str, where: str) -> str:
    if len(text) <= REVIEW_MAX_CHARS:
        return text
    return (text[:REVIEW_MAX_CHARS].rstrip()
            + f"\n… [cut at {REVIEW_MAX_CHARS} characters — the whole text: {where}]")


def _fence(text: str, lang: str = "") -> str:
    # A body that itself holds ``` would close the fence early.
    return f"```{lang}\n{(text or '').replace('```', 'ʼʼʼ')}\n```"


def review_text(item: dict, body: str, *, label: str, decide_line: str,
                is_skill: bool, active_body: Optional[str] = None) -> str:
    """The full review of ONE pending item (``body`` = its whole draft).

    A skill UPDATE is shown as a diff against the live skill — the change is
    what the owner decides on; a new item is shown whole. Fenced, so a
    SKILL.md's frontmatter and markup arrive exactly as written instead of
    rendered as chat formatting. ``label``/``decide_line`` come from the
    caller (``core.self_evolution``), which this module never imports back."""
    kind, item_id = item.get("kind", ""), str(item.get("id", ""))
    head = [f"Pending {label}"]
    if item.get("notes"):
        head.append(f"({item['notes']})")
    where = f"polyrob owner pending show {kind} {item_id}"
    if is_skill and active_body is not None:
        change = skill_change(body, active_body)
        diff = "\n".join(difflib.unified_diff(
            active_body.splitlines(), body.splitlines(),
            fromfile="live", tofile="proposed", lineterm="", n=2))
        lines = [" ".join(head),
                 f"Update: +{change['added']} / −{change['removed']} lines.",
                 _fence(_cap(diff or "(no line changes)", where), "diff")]
    else:
        if is_skill:
            head.append("— a new skill")
        lines = [" ".join(head), _fence(_cap(body, where))]
    lines.append(decide_line)
    return "\n".join(lines)
