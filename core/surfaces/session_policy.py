"""P0.1: session-boundary policy (pure) — a stable session-reset policy.

A chat keyed by a stable `session_key` (`agent:main:{surface}:{type}:{chat}[:{user}]`
— the `:{user}` segment is appended for a DM only; a room key carries no user segment,
see `core/surfaces/session_chat_registry.py::build_session_key`)
continues the SAME underlying session_id until a boundary fires; then the next message
starts a fresh session. Boundaries:
  - idle:  `now - last_activity > idle_minutes`. Last activity is the newer of
           `row.updated_at` (bumped on every STEER via SessionChatRegistry.touch)
           and `row.last_outbound_at` — the agent's own last line to the owner in
           this session (2026-10-13 review C3: the owner writes 08:00, the agent
           asks "which wallet?" at 10:00, the owner answers 20:30 — that is an
           answer to a 10:00 question, not a cold start). `with_agent_activity`
           fills `last_outbound_at` from the owner thread.
  - daily: the wall-clock crossed the local reset hour since last activity.
  - both:  whichever fires first (default).  none: never reset (legacy behavior).

Pure + `now` injected → unit-testable without a clock. Fail-safe: any odd input
returns (False, ...) so a policy bug can never spuriously wipe a live conversation.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


def _last_reset_instant(now_dt: datetime, daily_hour: int) -> datetime:
    """The most recent local daily-reset instant at or before `now_dt`."""
    candidate = now_dt.replace(hour=daily_hour, minute=0, second=0, microsecond=0)
    if candidate > now_dt:
        candidate -= timedelta(days=1)
    return candidate


def should_start_fresh(
    row: Optional[dict],
    *,
    now: float,
    idle_minutes: int,
    daily_hour: int,
    mode: str = "both",
) -> Tuple[bool, str]:
    """Return (start_fresh, reason). `row` is a session_chat_map row (needs updated_at)."""
    if not row or mode == "none":
        return (False, "disabled")
    last = row.get("updated_at")
    if last is None:
        return (False, "no-timestamp")
    try:
        last = float(last)
        now = float(now)
    except (TypeError, ValueError):
        return (False, "bad-timestamp")
    try:
        outbound = row.get("last_outbound_at")
        if outbound is not None and float(outbound) > last:
            last = min(float(outbound), now)
    except (TypeError, ValueError):
        pass  # an odd outbound stamp never decides anything

    if mode in ("idle", "both") and idle_minutes and idle_minutes > 0:
        if now - last > idle_minutes * 60:
            return (True, "idle")

    if mode in ("daily", "both"):
        try:
            now_dt = datetime.fromtimestamp(now)
            last_dt = datetime.fromtimestamp(last)
            reset = _last_reset_instant(now_dt, daily_hour)
            if last_dt < reset <= now_dt:
                return (True, "daily")
        except (OverflowError, OSError, ValueError):
            return (False, "bad-clock")

    return (False, "continue")


def with_agent_activity(row: Optional[dict], container: Any) -> Optional[dict]:
    """``row`` plus ``last_outbound_at``: the agent's newest line to the owner in
    this row's session, read from the owner thread (C3).

    Fail-open: any error (no store, no session id, an unreadable db) returns the
    row unchanged, so the idle boundary falls back to ``updated_at`` alone —
    exactly the pre-C3 behaviour. Never creates the store.
    """
    if not row or not row.get("session_id") or not row.get("user_id"):
        return row
    try:
        from core.surfaces.owner_thread import resolve_store
        store = resolve_store(container, create=False)
        if store is None:
            return row
        from core.sqlite_util import execute_retry
        found = execute_retry(
            store.db_path,
            "SELECT MAX(ts) AS t FROM owner_thread WHERE user_id=? AND session_id=? "
            "AND direction='out'",
            (str(row["user_id"]), str(row["session_id"])), fetch="one")
        ts = found["t"] if found is not None else None
        if ts is None:
            return row
        enriched = dict(row)
        enriched["last_outbound_at"] = float(ts)
        return enriched
    except Exception:
        logger.debug("session policy: agent-activity read skipped (fail-open)", exc_info=True)
        return row
