"""DM conversation-list watermark (2026-10-14 review C-11).

A DM sweep read 19 X Chat threads one by one (7 calls, 0.51M tokens) to learn
"nothing new", because the conversation list carried no last-message time.
``twitter_get_dms`` now returns each conversation with ``last_event_at``, a
``watermark`` and, given ``since``, only the threads newer than it.
"""
from __future__ import annotations

from typing import Optional


def _iso_ts(value) -> Optional[float]:
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def dm_list_since(conversations, since: Optional[str]):
    """``(conversations, watermark, dropped)`` for a DM conversation list
    (2026-10-14 review C-11: a sweep read 19 threads one by one for "nothing
    new"). Each row gets ``last_event_at`` (X's ``updated_at``, else
    ``created_at``; None when X sent neither); rows sort newest first; with
    ``since`` a row whose time is known and not newer is dropped (counted). A
    row with NO time is kept — unknown is not "old". ``watermark`` is the
    newest ``last_event_at`` seen (keep it for the next sweep's ``since``)."""
    cutoff = _iso_ts(since)
    out, dropped, newest = [], 0, None
    for c in conversations or []:
        row = dict(c) if isinstance(c, dict) else {"value": c}
        last = row.get("updated_at") or row.get("created_at")
        row["last_event_at"] = last
        ts = _iso_ts(last)
        if ts is not None and (newest is None or ts > newest[0]):
            newest = (ts, last)
        if cutoff is not None and ts is not None and ts <= cutoff:
            dropped += 1
            continue
        out.append(row)
    out.sort(key=lambda r: _iso_ts(r.get("last_event_at")) or float("inf"), reverse=True)
    return out, (newest[1] if newest else since), dropped
