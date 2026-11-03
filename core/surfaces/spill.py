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
console link — shipped in ``scripts/ops/ops_alert.py`` (the ops sidecar) and never
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

from core.env import bool_env

logger = logging.getLogger(__name__)

#: Directory (under the data home) holding spilled report bodies.
OUTBOX_DIRNAME = "outbox"

#: Spilled reports older than this are pruned on the next write.
_RETENTION_DAYS = 14

#: Lines worth lifting into the gist wherever they appear in the body — the
#: console deep link is the single most useful thing in a completion push, and
#: it is always last, so a naive head-truncation would drop exactly it.
_KEEP_LINE_RE = re.compile(r"^\s*(Console|Session|Link|URL)\s*:\s*\S+", re.IGNORECASE)


#: A tappable owner token (``/approve_p_<hex>``, ``/pending_tap_<id>``,
#: ``/fulfill_<id>_a``, ``/card_<id>_<act>``). A review or a list ends with its
#: decision taps; a head-truncation would drop exactly the line the owner acts
#: on, so a tail line carrying one is lifted into the gist like a link.
_TAP_TOKEN_RE = re.compile(r"(?:^|\s)/(?:approve|reject|pending|fulfill|card)_[A-Za-z0-9_]+")

#: Internal tuning (not env-driven): spill threshold and gist budget.
USER_DELIVERY_SPILL_CHARS = 1200
USER_DELIVERY_GIST_CHARS = 600

#: 096 W5 (owner decision D2): text the owner ASKED for — a command reply, a
#: ``/pending_p_<hex>`` review, a list — is read, not glanced at, so it spills
#: only above three Telegram messages (3 x ~4000), and its gist is one message.
OWNER_REPLY_SPILL_CHARS = 12000
OWNER_REPLY_GIST_CHARS = 3500

#: Telegram's caption hard cap is 1024; a one-line summary stays well under it.
_CAPTION_CHARS = 200


def spill_enabled() -> bool:
    """Master gate for the long-body spill (default ON)."""
    return bool_env("USER_DELIVERY_SPILL", True)


def spill_threshold_chars() -> int:
    """Body length above which the rail spills to an attachment.

    Deliberately well under a single Telegram message (4096) — the goal is a
    body the owner reads at a glance, not one that merely avoids chunking.
    """
    return max(200, USER_DELIVERY_SPILL_CHARS)


def gist_chars() -> int:
    """Character budget for the gist that replaces a spilled body."""
    return max(120, USER_DELIVERY_GIST_CHARS)


def _open_fence(lines: List[str]) -> Optional[str]:
    """The fence marker a run of *lines* leaves open, or None when balanced."""
    fence: Optional[str] = None
    for ln in lines:
        m = re.match(r"^\s*(`{3,}|~{3,})", ln)
        if not m:
            continue
        mark = m.group(1)
        if fence is None:
            fence = mark
        elif mark[0] == fence[0] and len(mark) >= len(fence):
            fence = None
    return fence


def build_gist(body: str, *, attachment_name: Optional[str] = None,
               limit: Optional[int] = None) -> str:
    """Lead with the outcome, keep the links and the taps, name the attachment.

    Takes whole leading lines within the budget (never a mid-sentence cut), then
    re-appends any link or tap line from the tail that the truncation would have
    lost. A code fence the head opened is closed before the tail (096 D2) — an
    unclosed fence turns the rest of the message into code.
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
        head_len = len(kept)
        fence = _open_fence(kept)
        if fence is not None:
            kept.append(fence)
        # Links live at the end of a completion push and taps at the end of a
        # review; a head-truncation drops the element that makes it actionable.
        # Bounded by the same budget again so a long tap list cannot regrow
        # the body the spill exists to shorten.
        extra = 0
        for ln in lines[head_len:]:
            if not (_KEEP_LINE_RE.match(ln) or _TAP_TOKEN_RE.search(ln)):
                continue
            if ln in kept or extra + len(ln) + 1 > budget:
                continue
            kept.append(ln)
            extra += len(ln) + 1
        if attachment_name:
            kept.append(f"📎 The whole text is attached: {attachment_name}")
        else:
            kept.append("(truncated)")
    return "\n".join(kept).strip()


def caption_for(body: str) -> str:
    """A one-line summary of *body* for the attachment caption: its first line
    with text, markdown markers stripped, cut on a word with a marker."""
    for ln in (body or "").splitlines():
        line = re.sub(r"[*_`#>]+", "", ln).strip(" -•\t")
        if not line:
            continue
        if len(line) <= _CAPTION_CHARS:
            return line
        cut = line[:_CAPTION_CHARS].rsplit(" ", 1)[0] or line[:_CAPTION_CHARS]
        return cut.rstrip() + " …"
    return ""


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
                now: Optional[float] = None,
                caption: Optional[str] = None) -> Optional[dict]:
    """Write *body* to a ``.txt`` file and return an attachable media entry.

    ``.txt`` (096 D5): a phone previews a text file inline; ``.md`` opened as an
    unknown type. The caption is a one-line summary (default: :func:`caption_for`).

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
        name = f"{safe_source or 'report'}-{stamp}-{digest}.txt"
        path = os.path.join(outbox, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        cap = caption if caption is not None else caption_for(body)
        return {"kind": "document", "path": path, "caption": cap or None}
    except OSError:
        logger.warning("spill: could not write report body", exc_info=True)
        return None


def maybe_spill(body: str, *, home_dir: str, source: str,
                now: Optional[float] = None,
                threshold: Optional[int] = None,
                gist_limit: Optional[int] = None,
                caption: Optional[str] = None) -> Optional[tuple]:
    """``(gist, media_entry)`` when *body* should spill, else None.

    None means "send it as-is" — a short body, the gate off, or a write failure.
    ``threshold``/``gist_limit`` default to the proactive rail's budget; an
    owner-requested reply passes :data:`OWNER_REPLY_SPILL_CHARS` /
    :data:`OWNER_REPLY_GIST_CHARS` (see :func:`maybe_spill_owner_reply`).
    """
    if not spill_enabled():
        return None
    limit = spill_threshold_chars() if threshold is None else max(200, int(threshold))
    if len(body or "") <= limit:
        return None
    entry = write_spill(body, home_dir=home_dir, source=source, now=now,
                        caption=caption)
    if entry is None:
        return None
    gist = build_gist(body, attachment_name=os.path.basename(entry["path"]),
                      limit=gist_limit)
    if not gist:
        return None
    return gist, entry


def maybe_spill_owner_reply(body: str, *, home_dir: str, source: str,
                            now: Optional[float] = None) -> Optional[tuple]:
    """096 W5: the same spill for text the owner asked for (a command reply,
    a review, a list) — above three messages it arrives as a one-message gist
    plus the whole text as an attached ``.txt``."""
    return maybe_spill(body, home_dir=home_dir, source=source, now=now,
                       threshold=OWNER_REPLY_SPILL_CHARS,
                       gist_limit=OWNER_REPLY_GIST_CHARS)
