"""§A40 / A7 — the owner's ONE answer to "what did I miss?"

The delivery rail (``core.surfaces.user_delivery.deliver_user_message``) never
silently drops a message it could not send live — it durably records an
``owner_notice`` event instead, marker-prefixed by shape
(``user_delivery.NOTICE_MARKERS``): suppressed by the daily cap, held by an
active owner pause, undelivered (no live sink / send failed), or suppressed by
the hourly rate limit.

What is NOT here is deliberate: framework lifecycle chatter (``▶ goal started``
and friends) never writes a notice at all. It is ephemeral status, and on prod
it was 822 of the 899 rows — four of the five entries this renders were noise
while the one report the owner needed sat underneath them.

Before this module existed, Telegram's ``/missed`` carried its own inline
query matching ONLY the cap marker — a paused or undelivered notice was
unreadable anywhere, and no other seat (CLI, REPL) could read notices at all.
This is the ONE query every seat reuses; ``surfaces/telegram/harness.py``,
``cli/commands/owner.py`` and ``cli/ui/commands/h_owner.py`` are all thin
renderers over it.

Layering: this module imports ``core.*`` only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import textwrap
import time
from typing import Dict, List, Optional

from core.sqlite_util import execute_retry
from core.surfaces.user_delivery import NOTICE_MARKERS

logger = logging.getLogger(__name__)

#: Marker -> the short "kind" word surfaced to the owner. Every entry of
#: ``NOTICE_MARKERS`` must appear here — an unmatched marker is a notice the
#: rail wrote and no seat can render (pinned by
#: ``tests/unit/core/surfaces/test_user_delivery_budget.py``).
_KIND_BY_MARKER = {
    NOTICE_MARKERS[0]: "capped",
    NOTICE_MARKERS[1]: "paused",
    NOTICE_MARKERS[2]: "undelivered",
    NOTICE_MARKERS[3]: "rate-limited",
    NOTICE_MARKERS[4]: "cooldown",
}


def _kind_for(text: str) -> str:
    for marker, kind in _KIND_BY_MARKER.items():
        if text.startswith(marker):
            return kind
    return "capped"  # unreachable given the WHERE clause below; a safe default


def _resolve_db_path(data_dir: str) -> str:
    """The ONE telemetry-db resolution (``core.event_log.telemetry_db_path``)."""
    from core.event_log import telemetry_db_path
    return telemetry_db_path(data_dir)


#: The most notices one count reads (a bound on the scan, far above a real day).
_COUNT_SCAN = 2000


def _missed_rows(path: str, user_id: str, since_ts: Optional[float], limit: int):
    """The ONE selection both the list and the count use: undelivered owner
    notices (every ``NOTICE_MARKERS`` marker — derived, never a hand list: a
    marker added to the rail without a clause here is a notice this query
    cannot see), newest first, minus every body a LATER ``sent`` row covers
    (D50: a body the rail retried and delivered minutes later was NOT missed)."""
    like_clause = " OR ".join(["attrs LIKE ?"] * len(NOTICE_MARKERS))
    params: list = [user_id, *[f"%{m}%" for m in NOTICE_MARKERS]]
    since_sql = ""
    if since_ts is not None:
        since_sql = " AND ts >= ?"
        params.append(float(since_ts))
    rows = execute_retry(
        path,
        "SELECT ts, attrs FROM telemetry_events WHERE kind='owner_notice' "
        f"AND user_id=? AND ({like_clause}){since_sql} "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (*params, int(limit)), fetch="all") or []
    for r in rows:
        try:
            attrs = json.loads(r["attrs"]) or {}
        except Exception:
            attrs = {}
        if _delivered_later(path, user_id, attrs.get("content_hash"), float(r["ts"])):
            continue
        yield float(r["ts"]), attrs


def missed_notices(user_id: str, data_dir: str, n: int = 5,
                   since_ts: Optional[float] = None) -> List[Dict]:
    """The last *n* undelivered owner notices for *user_id*, newest first
    (only those at or after ``since_ts`` when given).

    Each entry: ``{"ts": float, "text": str, "kind": "capped"|"paused"|"undelivered"}``
    — the marker prefix is stripped from ``text``.

    Raises on an unreadable telemetry store — this NEVER returns ``[]`` for
    "could not read"; callers render the reason (an empty list means "read
    fine, nothing to show").
    """
    path = _resolve_db_path(data_dir)
    if not os.path.exists(path):
        raise FileNotFoundError(f"telemetry log not found at {path}")
    limit = max(int(n), 1)
    out: List[Dict] = []
    # Over-read (limit * 4): the delivered-later filter drops rows.
    for ts, attrs in _missed_rows(path, user_id, since_ts, limit * 4):
        text = str(attrs.get("text") or "")
        kind = _kind_for(text)
        # Strip the rail's marker prefix "[<marker>; source=x] ".
        if text.startswith("[") and "] " in text:
            text = text.split("] ", 1)[1]
        out.append({"ts": ts, "text": text.strip(), "kind": kind})
        if len(out) >= limit:
            break
    return out


def missed_count(user_id: str, data_dir: str, since_ts: Optional[float] = None) -> int:
    """How many notices :func:`missed_notices` would list with no limit — the
    SAME selection (116 W5.6: the header counted cap-suppressed rows of the
    last 24 h while the list read every marker of all time). Raises like it."""
    path = _resolve_db_path(data_dir)
    if not os.path.exists(path):
        raise FileNotFoundError(f"telemetry log not found at {path}")
    return sum(1 for _ in _missed_rows(path, user_id, since_ts, _COUNT_SCAN))


def missed_since(user_id: str, data_dir: str, default_since: float) -> tuple:
    """``(since_ts, label)`` for a status seat: everything since the owner last
    ran ``/missed`` (he has read what came before), else the default window."""
    # The owner_command event (core/surfaces/owner_verb_log.py) read here with
    # this module's own connection helper: an import of that module would pull
    # it into the core import cycle.
    last = None
    try:
        path = _resolve_db_path(data_dir)
        if os.path.exists(path):
            row = execute_retry(
                path,
                "SELECT MAX(ts) AS ts FROM telemetry_events WHERE kind='owner_command' "
                "AND user_id=? AND json_extract(attrs, '$.verb')='/missed' "
                "AND json_extract(attrs, '$.outcome')='ok'", (str(user_id),), fetch="one")
            last = row["ts"] if row is not None else None
    except Exception:
        logger.warning("missed: the last /missed could not be read", exc_info=True)
    if last is not None:
        return float(last), "since your last /missed"
    return float(default_since), "in 24h"


def cap_caused(rows: List[Dict]) -> bool:
    """True when at least one of *rows* was suppressed by the daily CAP.

    080 (D F-18): every seat closed ``/missed`` with "raise the cap" — under
    an UNDELIVERED notice (no live sink, a send that failed) that is the wrong
    cause and a remedy that changes nothing. Seats offer it only when this
    says so.
    """
    return any(str(r.get("kind") or "") == "capped" for r in rows or ())


def _delivered_later(path: str, user_id: str, content_hash, ts: float) -> bool:
    """True when this notice's body was later DELIVERED to the same tenant.

    Keys on the ``content_hash`` the rail stamps on the notice row
    (``user_delivery._record_notice``). A legacy notice written before that
    stamp existed carries no hash — it is KEPT, because "I cannot tell whether
    you got this" must resolve toward showing the owner the message, never
    toward hiding it.

    A read fault is the same: keep the entry.
    """
    if not content_hash:
        return False
    try:
        row = execute_retry(
            path,
            "SELECT 1 FROM telemetry_events WHERE kind='user_delivery' "
            "AND user_id=? AND ts > ? "
            "AND json_extract(attrs, '$.outcome')='sent' "
            "AND json_extract(attrs, '$.content_hash')=? LIMIT 1",
            (user_id, ts, str(content_hash)), fetch="one")
    except Exception:
        return False
    return row is not None


def format_notice_lines(ts: float, kind: str, text: str, *, gutter: str = "",
                        width: int = 80) -> List[str]:
    """Render one ``/missed`` entry as ``["MM-DD HH:MMZ — [kind] <text…>",
    "     <text continues>", ...]`` — WRAPPED, never clipped.

    Fix round 1 (2026-09-14): a prior version hard-truncated the line at 77
    chars + ``"..."``. ``/missed`` exists to let the owner recover text they
    never received live — clipping it hides exactly the content it exists to
    show. The 80-column rule is about a default view fitting a terminal, not
    about losing content, so every line stays ``<= width`` columns and the
    FULL notice is still shown, wrapped across as many lines as it needs.

    ``gutter`` is a leading indent (e.g. two spaces, or ``candy.GUTTER``)
    shared by the CLI and REPL renderers, which both call this. Telegram's
    ``_missed_reply`` calls it too (``width=60``, its transport re-wraps) —
    it used to keep its own 240-char CLIP, which hid exactly the content
    ``/missed`` exists to recover. Every seat now renders through this ONE
    helper, so none of them can quietly truncate a recovered message again.
    """
    stamp = time.strftime("%m-%d %H:%M", time.gmtime(float(ts or 0)))
    prefix = f"{gutter}{stamp}Z — [{kind}] "
    indent = " " * len(prefix)
    # 096 W6 (D3): wrap PER LINE. One ``textwrap.wrap`` over the whole text
    # replaced every newline with a space, so a recovered message's bullets,
    # paragraphs and code fences collapsed into one run of prose. A fenced
    # line is code: kept whole, never re-wrapped.
    out: List[str] = []
    in_fence = False
    for raw in (text or "").strip("\n").split("\n"):
        lead = prefix if not out else indent
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            out.append((lead + raw).rstrip())
            continue
        if in_fence or not raw.strip():
            out.append((lead + raw).rstrip())
            continue
        bullet = _BULLET_LEAD_RE.match(raw)
        hang = indent + " " * (len(bullet.group(0)) if bullet else 0)
        out.extend(textwrap.wrap(raw, width=width, initial_indent=lead,
                                 subsequent_indent=hang, replace_whitespace=False)
                   or [lead.rstrip()])
    return out or [prefix.rstrip()]


#: A list item's marker (``- ``, ``• ``, ``1. ``) — its wrapped lines hang under the text.
_BULLET_LEAD_RE = re.compile(r"^[ \t]*(?:[-*+•]|\d{1,3}[.)])[ \t]+")
