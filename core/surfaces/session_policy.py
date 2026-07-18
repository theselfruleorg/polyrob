"""P0.1: session-boundary policy (pure) — a stable session-reset policy.

A chat keyed by a stable `session_key` (`agent:main:{surface}:{type}:{chat}:{user}`)
continues the SAME underlying session_id until a boundary fires; then the next message
starts a fresh session. Boundaries:
  - idle:  `now - row.updated_at > idle_minutes` (last-activity based; updated_at is
           bumped on every STEER via SessionChatRegistry.touch).
  - daily: the wall-clock crossed the local reset hour since last activity.
  - both:  whichever fires first (default).  none: never reset (legacy behavior).

Pure + `now` injected → unit-testable without a clock. Fail-safe: any odd input
returns (False, ...) so a policy bug can never spuriously wipe a live conversation.
"""
from datetime import datetime, timedelta
from typing import Optional, Tuple


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
