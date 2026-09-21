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
import os
import textwrap
import time
from typing import Dict, List

from core.sqlite_util import execute_retry
from core.surfaces.user_delivery import NOTICE_MARKERS

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


def missed_notices(user_id: str, data_dir: str, n: int = 5) -> List[Dict]:
    """The last *n* undelivered owner notices for *user_id*, newest first.

    Each entry: ``{"ts": float, "text": str, "kind": "capped"|"paused"|"undelivered"}``
    — the marker prefix is stripped from ``text``.

    Raises on an unreadable telemetry store — this NEVER returns ``[]`` for
    "could not read"; callers render the reason (an empty list means "read
    fine, nothing to show").
    """
    path = _resolve_db_path(data_dir)
    if not os.path.exists(path):
        raise FileNotFoundError(f"telemetry log not found at {path}")
    # Derived from NOTICE_MARKERS, not a hand-written list of three: a marker
    # added to the rail without a matching clause here is a notice this query
    # silently cannot see.
    like_clause = " OR ".join(["attrs LIKE ?"] * len(NOTICE_MARKERS))
    # D50: over-read, then drop the entries a LATER `sent` row covers, then trim
    # back to n. `/missed` answers "what did I miss" — a body the rail retried
    # and delivered minutes later was NOT missed, and showing it pushes a
    # genuinely lost entry off the five-row view.
    limit = max(int(n), 1)
    rows = execute_retry(
        path,
        "SELECT ts, attrs FROM telemetry_events WHERE kind='owner_notice' "
        f"AND user_id=? AND ({like_clause}) "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (user_id, *[f"%{m}%" for m in NOTICE_MARKERS], limit * 4),
        fetch="all") or []
    out: List[Dict] = []
    for r in rows:
        try:
            attrs = json.loads(r["attrs"]) or {}
        except Exception:
            attrs = {}
        text = str(attrs.get("text") or "")
        kind = _kind_for(text)
        # Strip the rail's marker prefix "[<marker>; source=x] ".
        if text.startswith("[") and "] " in text:
            text = text.split("] ", 1)[1]
        text = text.strip()
        if _delivered_later(path, user_id, attrs.get("content_hash"),
                            float(r["ts"])):
            continue
        out.append({"ts": float(r["ts"]), "text": text, "kind": kind})
        if len(out) >= limit:
            break
    return out


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
    wrapped = textwrap.wrap(text, width=width, initial_indent=prefix,
                            subsequent_indent=" " * len(prefix))
    return wrapped or [prefix.rstrip()]
