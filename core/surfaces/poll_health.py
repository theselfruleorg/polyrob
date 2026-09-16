"""Durable record of inbound TRANSPORT faults on a polling surface.

2026-09-15 prod review, C11. Over 7 days prod logged 76
``telegram get_updates failed`` errors — 34 request timeouts, 28 connection
resets, 14 Bad Gateway — and no status seat could report that the agent had
been intermittently unreachable. The poller recovers every time, and that is
exactly why nothing surfaced it: **recovery is not the same as health.**

One helper, shared by every polling surface (telegram long-poll today, the IMAP
fetcher next), so a second transport never invents a second answer.
``core.status_snapshot._surface_poll_health`` is the consumer.
"""
from __future__ import annotations

import logging
import time
from typing import Dict

logger = logging.getLogger(__name__)

#: Minimum seconds between two recorded rows PER SURFACE. A sustained outage
#: retries about once a second; the point is that the owner can SEE the
#: instability, not that every retry becomes a row.
RECORD_EVERY_SEC = 60.0

_last_recorded: Dict[str, float] = {}


def record_poll_error(surface_id: str, exc: BaseException, *,
                      now: float | None = None) -> bool:
    """Record one inbound poll failure. Returns whether a row was written.

    Rate-limited per surface and fail-open: a telemetry fault must never reach
    the poll loop, which is the thread keeping the agent reachable at all.
    """
    sid = str(surface_id or "?")
    ts = time.time() if now is None else now
    try:
        if ts - _last_recorded.get(sid, 0.0) < RECORD_EVERY_SEC:
            return False
        from core.event_kinds import SURFACE_POLL_ERROR
        from core.event_log import event_log_enabled, get_event_log
        if not event_log_enabled():
            _last_recorded[sid] = ts
            return False
        get_event_log().record(SURFACE_POLL_ERROR, source=sid,
                               attrs={"error": f"{type(exc).__name__}: {exc}"[:200]})
        _last_recorded[sid] = ts
        return True
    except Exception:
        logger.debug("poll-error record skipped for %s", sid, exc_info=True)
        return False


def reset_for_tests() -> None:
    """Clear the per-surface rate-limit clock (tests only)."""
    _last_recorded.clear()
