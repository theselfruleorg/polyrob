"""The one read of the telemetry log as a view: counts by kind, wallet spend, the
newest rows (120 W3).

The terminal's ``/telemetry`` held this read inside its handler, where the agent
could not reach it. It lives here once; the REPL renders it as tables and
``agent_status(view="log")`` renders it as lines (:func:`log_view_lines`).

Rules this read keeps:

- A read never creates the store (:func:`core.event_log.open_event_log`). An
  absent or unreadable store RAISES; the caller names it, never an empty list.
- Tenant: ``user_id=None`` is every row (the terminal operator's view, as before);
  a tenant reads its own rows, plus the instance's untenanted rows only when
  ``instance_rows`` (the owner).
- Rows carry third-party text (a sender's preview, a tool argument). The agent's
  render caps the row count and the row length, and frames the rows as data.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

#: The rows the agent's view shows when none is named, the most one read shows,
#: and the longest detail of one row.
LOG_DEFAULT_ROWS = 10
LOG_MAX_ROWS = 50
LOG_DETAIL_CHARS = 160


def event_row(record: Dict[str, Any]) -> Tuple[str, str, str]:
    """``(kind, detail, session)`` of one event row; ``""`` where the row has none.
    The session id keeps its 12-char prefix: a correlation hint, not a handle."""
    attrs = record.get("attrs") or {}
    detail = (attrs.get("outcome") or attrs.get("action") or attrs.get("reason")
              or attrs.get("preview") or "")
    return (str(record.get("kind") or ""), str(detail),
            str(record.get("session_id") or "")[:12])


def telemetry_view(*, data_dir: Optional[str] = None, user_id: Optional[str] = None,
                   instance_rows: bool = False, since_ts: Optional[float] = None,
                   limit: int = LOG_DEFAULT_ROWS) -> Dict[str, Any]:
    """``{"counts", "total", "wallet_spend_usd", "recent"}`` — ``recent`` is newest
    first, each row the raw record. RAISES when the store is absent or cannot be
    read."""
    from core.event_log import open_event_log, telemetry_db_path
    log = open_event_log(data_dir)
    if log is None:
        raise FileNotFoundError(f"telemetry_events.db not found at {telemetry_db_path(data_dir)}")
    agg = log.aggregate(since_ts=since_ts, user_id=user_id, instance_rows=instance_rows)
    recent = log.query(since_ts=since_ts, user_id=user_id, instance_rows=instance_rows,
                       limit=max(1, int(limit)), strict=True)
    return {"counts": dict(agg.get("counts_by_kind") or {}),
            "total": int(agg.get("total_events") or 0),
            "wallet_spend_usd": float(agg.get("wallet_spend_usd") or 0.0),
            "recent": recent}


def log_view_lines(view: Dict[str, Any], *, scope: str, show_spend: bool = True,
                   now: Optional[float] = None) -> Tuple[List[str], List[str]]:
    """The agent's render: ``(totals, rows)``. ``totals`` is harness text; ``rows``
    carries third-party text, so the caller frames it as data. ``show_spend=False``
    withholds the wallet spend (the wallet is the owner's)."""
    now = time.time() if now is None else now
    totals = [f"log — {scope}: {view['total']} events"]
    for kind, n in sorted(view["counts"].items(), key=lambda kv: (-kv[1], kv[0])):
        totals.append(f"  {kind}: {n}")
    totals.append(f"  wallet spend: ${view['wallet_spend_usd']:.4f}" if show_spend
                  else "  wallet spend: withheld (the wallet is the owner's)")
    rows = []
    for record in view["recent"]:
        kind, detail, session = event_row(record)
        detail = " ".join(detail.split())
        if len(detail) > LOG_DETAIL_CHARS:
            detail = detail[:LOG_DETAIL_CHARS - 1] + "…"
        age = max(0, int(now - float(record.get("ts") or 0.0)))
        ago = (f"{age // 86400}d" if age >= 86400 else f"{age // 3600}h" if age >= 3600
               else f"{age // 60}m" if age >= 60 else f"{age}s")
        rows.append(f"{ago} ago · {kind or '-'}" + (f" · {detail}" if detail else "")
                    + (f" · session {session}" if session else ""))
    return totals, rows


__all__ = ["LOG_DEFAULT_ROWS", "LOG_MAX_ROWS", "event_row", "log_view_lines",
           "telemetry_view"]
