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
  Why, the named changes, then a diff against the live text; a new item whole.
* :func:`change_summary` (096 W2) — what a draft CHANGES, in the owner's
  words: a field, a section edited word for word, reworded, added, removed.

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
            "description": description,
            "parts": change_summary(new_body, active_body)["parts"]}


# --- 096 W2: describe the CHANGE, not the new text ---------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_WHEN_RE = re.compile(r"^\s*polyrob-when:\s*(.*)$", re.MULTILINE)
#: A "small edit" is shown word for word: at most this many changed words a side.
_SMALL_EDIT_WORDS = 12
_SNIPPET_CHARS = 90
#: The notice names at most this many changes, then "+N more".
NOTICE_MAX_PARTS = 3


def _sections(text: str) -> "list[tuple[str, str]]":
    """``[(heading, body)]`` in order; text before the first heading is ``""``.
    A repeated heading gets a ``(2)`` suffix so each section has one name."""
    out: list = []
    name, buf, seen = "", [], {}
    for line in (text or "").splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if name or "".join(buf).strip():
                out.append((name, "\n".join(buf)))
            title = m.group(2).strip()
            seen[title] = seen.get(title, 0) + 1
            name = title if seen[title] == 1 else f"{title} ({seen[title]})"
            buf = []
        else:
            buf.append(line)
    if name or "".join(buf).strip():
        out.append((name, "\n".join(buf)))
    return out


def _sect_label(name: str) -> str:
    return f"§{name}" if name else "the opening text"


def _snip(words: "list[str]") -> str:
    text = " ".join(words)
    return text if len(text) <= _SNIPPET_CHARS else text[:_SNIPPET_CHARS - 1].rstrip() + "…"


def word_edits(old: str, new: str) -> Optional["list[tuple[str, str]]"]:
    """``[(old words, new words)]`` when *old*→*new* is a SMALL word-level edit
    (at most two changed runs, each ≤ ``_SMALL_EDIT_WORDS`` words a side), else
    ``None``. One context word either side keeps an insertion readable. Pure."""
    a, b = (old or "").split(), (new or "").split()
    ops = [op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
           if op[0] != "equal"]
    if not ops or len(ops) > 2:
        return None if ops else []
    out = []
    for _tag, i1, i2, j1, j2 in ops:
        if i2 - i1 > _SMALL_EDIT_WORDS or j2 - j1 > _SMALL_EDIT_WORDS:
            return None
        lo, hi = max(i1 - 2, 0), min(i2 + 1, len(a))
        nlo, nhi = max(j1 - 2, 0), min(j2 + 1, len(b))
        out.append((_snip(a[lo:hi]), _snip(b[nlo:nhi])))
    return out


def _edit_text(edits: "list[tuple[str, str]]") -> str:
    return "; ".join(f'"{o}" → "{n}"' for o, n in edits)


def _section_change(name: str, old: str, new: str) -> str:
    label = _sect_label(name)
    edits = word_edits(old, new)
    if edits == []:
        return f"{label} re-wrapped (same words)"
    if edits:
        return f"{label}: {_edit_text(edits)}"
    ratio = difflib.SequenceMatcher(None, old.split(), new.split(), autojunk=False).ratio()
    return f"{'reworded' if ratio >= 0.6 else 'rewrote'} {label}"


def _meta_when(text: str) -> str:
    m = _WHEN_RE.search(text or "")
    return m.group(1).strip().strip("'\"") if m else ""


def change_summary(new_body: str, live_body: Optional[str]) -> dict:
    """What a draft CHANGES against the live text, in the owner's words.

    ``{"new", "parts", "added", "removed"}`` — ``parts`` names each change:
    a frontmatter field ("description reworded: "a" → "b""), a section edited
    word for word, a section reworded / rewritten, a section added or removed.
    A reflowed paragraph is "re-wrapped (same words)", never a −25/+20 line
    count. ``added``/``removed`` are non-blank line counts, for the review.
    Pure; markdown-aware, and fine on a doc with no headings."""
    new_body = new_body or ""
    if live_body is None:
        return {"new": True, "parts": [], "removed": 0,
                "added": sum(1 for ln in new_body.splitlines() if ln.strip())}
    old_meta, old_rest = split_frontmatter(live_body)
    new_meta, new_rest = split_frontmatter(new_body)
    parts: list = []
    for key in ("name", "description"):
        o, n = old_meta.get(key, ""), new_meta.get(key, "")
        if o != n:
            edits = word_edits(o, n)
            parts.append(f"{key} changed: {_edit_text(edits)}" if edits
                         else f"{key} changed")
    ow, nw = _meta_when(live_body.split("\n---", 1)[0]), _meta_when(new_body.split("\n---", 1)[0])
    if ow != nw:
        parts.append(f'"when to load" line changed: "{ow}" → "{nw}"' if ow
                     else f'"when to load" line set: "{nw}"')
    old_s, new_s = _sections(old_rest), _sections(new_rest)
    old_map, new_map = dict(old_s), dict(new_s)
    for name, body in new_s:
        if name in old_map and " ".join(old_map[name].split()) != " ".join(body.split()):
            parts.append(_section_change(name, old_map[name], body))
        elif name in old_map and old_map[name] != body:
            parts.append(f"{_sect_label(name)} re-wrapped (same words)")
    parts += [f"added {_sect_label(n)}" for n, _b in new_s if n not in old_map]
    parts += [f"removed {_sect_label(n)}" for n, _b in old_s if n not in new_map]
    # A re-wrap changes no word: it goes last, after every change of meaning.
    parts.sort(key=lambda p: p.endswith("(same words)"))
    added = removed = 0
    for line in difflib.unified_diff(live_body.splitlines(), new_body.splitlines(),
                                     lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip():
            added += 1
        elif line.startswith("-") and not line.startswith("---") and line[1:].strip():
            removed += 1
    if not parts and (added or removed):
        parts.append(f"+{added} / −{removed} lines")
    return {"new": False, "parts": parts, "added": added, "removed": removed}


def parts_text(parts: "list[str]", limit: Optional[int] = NOTICE_MAX_PARTS) -> str:
    """``a; b; c; +N more`` — capped by MEANING (whole changes), never mid-word."""
    parts = [p for p in parts or [] if p]
    if limit is None or len(parts) <= limit:
        return "; ".join(parts)
    return "; ".join(parts[:limit]) + f"; +{len(parts) - limit} more"


def why_line(item: dict) -> str:
    """``Why: <the drafter's reason>`` — or says plainly that none was recorded."""
    reason = str(item.get("reason") or "").strip()
    if not reason and item.get("reason_unreadable"):
        return "Why: unreadable (the reasons store could not be read)"
    return f"Why: {reason}" if reason else "Why: not recorded (the draft gave no reason)"


def summary_line(item: dict) -> str:
    """The text after the item's label on a notice or list line.

    The author/link note (``item["notes"]``) is kept whole and the cap applies
    to the CONTENT only — the note used to eat the space the content needed."""
    notes = item.get("notes") or ""
    change = item.get("change")
    if isinstance(change, dict):
        # 096 W2: an update names what CHANGED; a new item says what it is.
        if change.get("new"):
            gist = change.get("description") or _preview_text(item, notes)
            body = (f"{'new skill' if item.get('kind') == 'skill' else 'new'} — "
                    f"{_one_line(gist, SUMMARY_MAX_CHARS)}")
        elif change.get("parts"):
            body = f"update — {parts_text(change['parts'])}"
        else:
            body = (f"update: +{change.get('added', 0)} / −{change.get('removed', 0)} lines"
                    + (f" — {_one_line(change['description'], SUMMARY_MAX_CHARS)}"
                       if change.get("description") else ""))
    else:
        body = _one_line(_preview_text(item, notes), SUMMARY_MAX_CHARS)
    return (f"[{notes}] {body}" if notes else body).strip()


def _preview_text(item: dict, notes: str) -> str:
    raw = str(item.get("preview") or "")
    if notes and raw.startswith(f"[{notes}]"):
        raw = raw[len(notes) + 2:]
    return raw


def _cap(text: str) -> Tuple[str, int]:
    """``(shown, hidden_chars)`` — the cut note goes OUTSIDE the fence."""
    if len(text) <= REVIEW_MAX_CHARS:
        return text, 0
    shown = text[:REVIEW_MAX_CHARS].rstrip()
    return shown, len(text) - len(shown)


def _cut_note(hidden: int, kind: str, item_id: str) -> str:
    # 096 A6: the pointer named ``polyrob owner pending show`` — a command that
    # does not exist — and sat inside the fence. The real command is
    # ``owner show-pending``; the console Inbox is the other seat with room.
    return (f"… {hidden} more characters not shown here. The whole text: "
            f"the console Inbox, or `polyrob owner show-pending {kind} {item_id}` "
            f"on the machine that runs me.")


def _fence(text: str, lang: str = "") -> str:
    # A body that itself holds ``` would close the fence early.
    return f"```{lang}\n{(text or '').replace('```', 'ʼʼʼ')}\n```"


def review_text(item: dict, body: str, *, label: str, decide_line: str,
                is_skill: bool, active_body: Optional[str] = None,
                extra_sections: Optional["list[str]"] = None) -> str:
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
    if active_body:
        # 096 W2: every kind with a live text is reviewed as a CHANGE — the
        # review cap now covers the diff, not the whole doc, so an owner-doc
        # change past char 12000 is visible.
        change = change_summary(body, active_body)
        diff = "\n".join(difflib.unified_diff(
            active_body.splitlines(), body.splitlines(),
            fromfile="live", tofile="proposed", lineterm="", n=2))
        shown, hidden = _cap(diff or "(no line changes)")
        lines = [" ".join(head), why_line(item), "Changes:"]
        lines += [f"• {p}" for p in change["parts"]] or ["• (no change in the text)"]
        lines += [f"(+{change['added']} / −{change['removed']} lines)", _fence(shown, "diff")]
    else:
        head.append("— a new skill" if is_skill else "— new")
        shown, hidden = _cap(body)
        lines = [" ".join(head), why_line(item), _fence(shown)]
    if hidden:
        lines.append(_cut_note(hidden, kind, item_id))
    lines.extend(extra_sections or [])
    lines.append(decide_line)
    return "\n".join(lines)
