"""Memory-store hygiene on the curator tick (B2, 2026-10-11).

:func:`autonomous_window_days` — the shorter age window for rows an autonomous
(cron/goal) run wrote. Prod wrote 250–1,200 such rows a day and the 365-day
window never deleted one; owner chat rows keep the long window.
"""

#: Rows an autonomous run wrote age out after this many days (capped by
#: MEMORY_RETENTION_DAYS when that is shorter). Not a flag on purpose.
AUTONOMOUS_RETENTION_DAYS = 45


def autonomous_window_days(general_days: int) -> int:
    """The autonomous-row window for a general window of *general_days*."""
    return max(1, min(AUTONOMOUS_RETENTION_DAYS, int(general_days)))
