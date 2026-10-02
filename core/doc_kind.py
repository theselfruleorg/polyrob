"""060 WS-3 — mark INSTRUCTION vs RECORD on workspace documents (2026-09-23).

On 2026-09-21 the agent correctly reasoned that ``x-post-queue.md`` is a dated
chronological log and that rewriting it would falsify history — by judgement
alone, with nothing on disk to read. This module is the marker and its reader:

    ---
    kind: instruction          # or: record
    supersedes: old-rules.md   # optional, comma-separated
    ---

- ``kind: instruction`` — a rule, checklist or living page, meant to be edited.
- ``kind: record`` — history (a log, a report, a receipt). It may be APPENDED
  to; it is never rewritten or deleted.
- **Undeclared = record** (owner decision Q2, 2026-09-23). The fail-safe
  direction: the cost of the wrong default is a refused edit the agent can
  resolve in one step, never a falsified history.

The refusal (``record_write_refusal``) is what the filesystem verbs call. It
exempts a file THIS session already wrote (the agent's own draft), and a write
whose only change is to ADD the ``kind: instruction`` declaration on top of the
unchanged body (the one-step way to declare a living document).

``DOC_KIND_ENFORCED`` (default ON) — ``false`` restores judgement-only writes.

Pure ``core`` (stdlib + ``core.env``).
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from core.env import bool_env

KIND_INSTRUCTION = "instruction"
KIND_RECORD = "record"
KINDS = (KIND_INSTRUCTION, KIND_RECORD)

#: Only these documents carry a kind. Code, data and binary files are never
#: records in this sense (their history is git's job).
DOC_SUFFIXES = (".md", ".markdown")

_FM_RE = re.compile(r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_KV_RE = re.compile(r"^\s*(?P<key>[A-Za-z_][\w-]*)\s*:\s*(?P<value>.*?)\s*$")


def doc_kind_enforced() -> bool:
    """``DOC_KIND_ENFORCED`` — refuse a rewrite of a RECORD. Default ON.
    Literal name on purpose (``test_flags_reverse`` scans for it)."""
    return bool_env("DOC_KIND_ENFORCED", True)


def is_doc_path(path: str) -> bool:
    return str(path or "").lower().endswith(DOC_SUFFIXES)


def parse_front_matter(text: str) -> Tuple[Dict[str, str], str]:
    """``(fields, body)``. No front-matter => ``({}, text)``. Keys lower-cased;
    only flat ``key: value`` lines are read (never a YAML parser)."""
    m = _FM_RE.match(text or "")
    if not m:
        return {}, text or ""
    fields: Dict[str, str] = {}
    for line in m.group("body").splitlines():
        kv = _KV_RE.match(line)
        if kv:
            fields[kv.group("key").lower()] = kv.group("value").strip().strip("'\"")
    return fields, (text or "")[m.end():]


def doc_kind(text: str) -> str:
    """The declared kind; anything undeclared or unknown is a RECORD."""
    fields, _body = parse_front_matter(text)
    kind = (fields.get("kind") or "").strip().lower()
    return kind if kind in KINDS else KIND_RECORD


def is_declared(text: str) -> bool:
    fields, _body = parse_front_matter(text)
    return (fields.get("kind") or "").strip().lower() in KINDS


def supersedes(text: str) -> List[str]:
    """The documents this one declares it supersedes (``supersedes: a.md, b.md``)."""
    fields, _body = parse_front_matter(text)
    raw = fields.get("supersedes") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def _is_declaration_only(old: str, new: str) -> bool:
    """``new`` = a front-matter block declaring ``kind: instruction`` + ``old``'s
    unchanged body (and ``old`` had no front-matter of its own)."""
    if parse_front_matter(old)[0]:
        return False
    fields, body = parse_front_matter(new)
    return (fields.get("kind") or "").lower() == KIND_INSTRUCTION and body.strip() == (old or "").strip()


def record_write_refusal(display_path: str, old: Optional[str], new: Optional[str], *,
                         written_this_session: bool = False) -> Optional[str]:
    """The refusal sentence for a write that would EDIT a record, or ``None``.

    ``old`` is the existing content (``None`` = no such file); ``new`` is the
    content the write would leave (``None`` = a delete). Allowed: a new file, an
    instruction doc, a pure append, a file this session already wrote, and the
    one-step ``kind: instruction`` declaration. Everything else on a record is
    refused with the remedy named.
    """
    if not doc_kind_enforced() or not is_doc_path(display_path):
        return None
    if old is None or not old.strip() or written_this_session:
        return None
    if doc_kind(old) == KIND_INSTRUCTION:
        return None
    if new is not None:
        if new.startswith(old):
            return None  # an append keeps every line of the history
        if _is_declaration_only(old, new):
            return None
    verb = "delete" if new is None else "rewrite"
    declared = "declared `kind: record`" if is_declared(old) else "undeclared, so a record"
    return (f"Refusing to {verb} {display_path}: it is a RECORD ({declared}) — dated "
            "history that a rewrite would falsify. Append to it (append_file), or write "
            "a NEW file. If it is really a rule, checklist or living page, declare it "
            "first: write the same content with the front-matter "
            "`---\\nkind: instruction\\n---` on top (that one change is allowed), then edit it.")


def classify_tree(root: str, *, max_files: int = 2000, max_depth: int = 6) -> Dict[str, object]:
    """Count the marked documents under ``root`` (bounded walk, read-only).

    Returns ``{"instruction": n, "record": n, "undeclared": n, "instruction_docs":
    [relpaths], "superseded": [relpaths]}``; ``{"state": reason}`` when ``root``
    is absent. A file that cannot be read is COUNTED as unreadable, never skipped.
    """
    if not root or not os.path.isdir(root):
        return {"state": "no persistent workspace found"}
    out: Dict[str, object] = {"instruction": 0, "record": 0, "undeclared": 0,
                              "unreadable": 0, "instruction_docs": [], "superseded": []}
    seen = 0
    superseded: set = set()
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")
                       and dirpath.count(os.sep) - base_depth < max_depth]
        for name in filenames:
            if not is_doc_path(name):
                continue
            seen += 1
            if seen > max_files:
                out["truncated"] = True
                break
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    head = fh.read(4096)
            except OSError:
                out["unreadable"] = int(out["unreadable"]) + 1
                continue
            if not is_declared(head):
                out["undeclared"] = int(out["undeclared"]) + 1
            elif doc_kind(head) == KIND_INSTRUCTION:
                out["instruction"] = int(out["instruction"]) + 1
                out["instruction_docs"].append(rel)  # type: ignore[union-attr]
                for s in supersedes(head):
                    superseded.add(os.path.normpath(os.path.join(os.path.dirname(rel), s)))
            else:
                out["record"] = int(out["record"]) + 1
        if out.get("truncated"):
            break
    out["superseded"] = sorted(superseded)
    return out


__all__ = [
    "KIND_INSTRUCTION", "KIND_RECORD", "KINDS", "DOC_SUFFIXES",
    "doc_kind_enforced", "is_doc_path", "parse_front_matter", "doc_kind",
    "is_declared", "supersedes", "record_write_refusal", "classify_tree",
]
