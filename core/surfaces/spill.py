"""Long-body spill: a chat message is not a document (chat-first review, G8).

The owner rail composes machine-generated blocks — goal completions, digests,
escalations — and hands them to a transport that faithfully splits a 6 KB body
into N phone messages. The transport is not the problem; composing a document
and calling it a message is.

This is the ONE policy that fixes it for every producer at once: over a
threshold, the body is written to a rendered artifact, attached, and the owner
gets a SHORT gist plus the attachment. It lives on the delivery rail rather than
in any single producer, so goal completions, cron delivery, escalations and
digests all inherit it without a call-site change.

The contract the 2026-08-21 owner directive asked for — gist + attachment +
console link — shipped in ``scripts/ops_alert.py`` (the ops sidecar) and never
reached the agent's own rail. This closes that gap.

Nothing here is best-effort-silent: a spill that cannot be written returns None
and the caller sends the full body exactly as before (fail-open to today).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import List, Optional

from core.env import bool_env, int_env

logger = logging.getLogger(__name__)

#: Directory (under the data home) holding spilled report bodies.
OUTBOX_DIRNAME = "outbox"

#: Spilled reports older than this are pruned on the next write.
_RETENTION_DAYS = 14

#: Lines worth lifting into the gist wherever they appear in the body — the
#: console deep link is the single most useful thing in a completion push, and
#: it is always last, so a naive head-truncation would drop exactly it.
_KEEP_LINE_RE = re.compile(r"^\s*(Console|Session|Link|URL)\s*:\s*\S+", re.IGNORECASE)


def spill_enabled() -> bool:
    """Master gate for the long-body spill (default ON)."""
    return bool_env("USER_DELIVERY_SPILL", True)


def spill_threshold_chars() -> int:
    """Body length above which the rail spills to an attachment.

    Deliberately well under a single Telegram message (4096) — the goal is a
    body the owner reads at a glance, not one that merely avoids chunking.
    """
    return max(200, int_env("USER_DELIVERY_SPILL_CHARS", 1200))


def gist_chars() -> int:
    """Character budget for the gist that replaces a spilled body."""
    return max(120, int_env("USER_DELIVERY_GIST_CHARS", 600))


def build_gist(body: str, *, attachment_name: Optional[str] = None,
               limit: Optional[int] = None) -> str:
    """Lead with the outcome, keep the links, name the attachment.

    Takes whole leading lines within the budget (never a mid-sentence cut), then
    re-appends any link line from the tail that the truncation would have lost.
    """
    budget = gist_chars() if limit is None else max(80, int(limit))
    lines = (body or "").splitlines()
    kept: List[str] = []
    used = 0
    cut = False
    for i, line in enumerate(lines):
        if used + len(line) + 1 > budget:
            cut = bool(lines[i:])
            break
        kept.append(line)
        used += len(line) + 1
    if not kept and lines:  # a single very long first line
        kept = [lines[0][:budget].rstrip()]
        cut = True
    if cut:
        # Links live at the end of a completion push; a head-truncation drops the
        # one element that makes the message actionable.
        tail = [ln for ln in lines[len(kept):] if _KEEP_LINE_RE.match(ln)]
        for ln in tail:
            if ln not in kept:
                kept.append(ln)
        if attachment_name:
            kept.append(f"📎 Full report attached: {attachment_name}")
        else:
            kept.append("(truncated)")
    return "\n".join(kept).strip()


def _outbox_dir(home_dir: str) -> str:
    return os.path.join(str(home_dir), OUTBOX_DIRNAME)


def _prune(outbox: str, *, now: float) -> None:
    cutoff = now - _RETENTION_DAYS * 86400
    try:
        for name in os.listdir(outbox):
            path = os.path.join(outbox, name)
            try:
                if os.path.isfile(path) and os.stat(path).st_mtime < cutoff:
                    os.remove(path)
            except OSError:
                continue
    except OSError:
        logger.debug("spill: outbox prune skipped", exc_info=True)


def write_spill(body: str, *, home_dir: str, source: str,
                now: Optional[float] = None) -> Optional[dict]:
    """Write *body* to a report file and return an attachable media entry.

    Returns None on any failure — the caller then sends the full body unchanged,
    so a broken spill degrades to today's behaviour rather than losing content.
    """
    ts = time.time() if now is None else now
    try:
        outbox = _outbox_dir(home_dir)
        os.makedirs(outbox, exist_ok=True)
        _prune(outbox, now=ts)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
        digest = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:8]
        safe_source = re.sub(r"[^A-Za-z0-9_-]+", "-", str(source or "report")).strip("-")
        name = f"{safe_source or 'report'}-{stamp}-{digest}.md"
        path = os.path.join(outbox, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return {"kind": "document", "path": path, "caption": None}
    except OSError:
        logger.warning("spill: could not write report body", exc_info=True)
        return None


def maybe_spill(body: str, *, home_dir: str, source: str,
                now: Optional[float] = None) -> Optional[tuple]:
    """``(gist, media_entry)`` when *body* should spill, else None.

    None means "send it as-is" — a short body, the gate off, or a write failure.
    """
    if not spill_enabled():
        return None
    if len(body or "") <= spill_threshold_chars():
        return None
    entry = write_spill(body, home_dir=home_dir, source=source, now=now)
    if entry is None:
        return None
    gist = build_gist(body, attachment_name=os.path.basename(entry["path"]))
    if not gist:
        return None
    return gist, entry
