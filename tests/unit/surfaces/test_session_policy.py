"""P0.1: session-boundary policy — should_start_fresh (pure, injected `now`).

Ports Hermes's SessionResetPolicy onto our session_chat_map row: a chat continues
the same session until it goes idle (>idle_minutes since last activity) or crosses
the daily reset hour, at which point the next message starts a fresh session.
"""
from datetime import datetime, timedelta

from core.surfaces.session_policy import should_start_fresh


def _epoch(dt: datetime) -> float:
    return dt.timestamp()


def test_mode_none_never_resets():
    row = {"updated_at": 0.0}
    fresh, _ = should_start_fresh(row, now=10_000_000.0, idle_minutes=1, daily_hour=4, mode="none")
    assert fresh is False


def test_no_row_does_not_reset():
    fresh, reason = should_start_fresh(None, now=1.0, idle_minutes=1, daily_hour=4, mode="both")
    assert fresh is False


def test_missing_timestamp_does_not_reset():
    fresh, _ = should_start_fresh({}, now=1.0, idle_minutes=1, daily_hour=4, mode="both")
    assert fresh is False


def test_idle_expiry_starts_fresh():
    now = 10_000_000.0
    row = {"updated_at": now - (61 * 60)}  # 61 min idle, threshold 60
    fresh, reason = should_start_fresh(row, now=now, idle_minutes=60, daily_hour=4, mode="idle")
    assert fresh is True and reason == "idle"


def test_within_idle_continues():
    now = 10_000_000.0
    row = {"updated_at": now - (59 * 60)}  # 59 min, under threshold
    fresh, _ = should_start_fresh(row, now=now, idle_minutes=60, daily_hour=4, mode="idle")
    assert fresh is False


def test_idle_zero_disables_idle_branch():
    now = 10_000_000.0
    row = {"updated_at": now - (10 * 86400)}  # 10 days idle
    fresh, _ = should_start_fresh(row, now=now, idle_minutes=0, daily_hour=4, mode="idle")
    assert fresh is False  # idle disabled


def test_daily_roll_crossing_reset_hour_starts_fresh():
    # last activity yesterday 23:00; now today 05:00; reset hour 04:00 -> crossed.
    now_dt = datetime(2026, 6, 24, 5, 0, 0)
    last_dt = datetime(2026, 6, 23, 23, 0, 0)
    row = {"updated_at": _epoch(last_dt)}
    fresh, reason = should_start_fresh(row, now=_epoch(now_dt), idle_minutes=0, daily_hour=4, mode="daily")
    assert fresh is True and reason == "daily"


def test_daily_no_roll_when_both_after_reset_hour():
    # last activity today 04:30, now today 05:00; reset hour 04:00 -> NOT crossed.
    now_dt = datetime(2026, 6, 24, 5, 0, 0)
    last_dt = datetime(2026, 6, 24, 4, 30, 0)
    row = {"updated_at": _epoch(last_dt)}
    fresh, _ = should_start_fresh(row, now=_epoch(now_dt), idle_minutes=0, daily_hour=4, mode="daily")
    assert fresh is False


def test_both_mode_idle_fires_even_before_daily():
    now = 10_000_000.0
    row = {"updated_at": now - (2 * 86400)}  # 2 days idle
    fresh, reason = should_start_fresh(row, now=now, idle_minutes=1440, daily_hour=4, mode="both")
    assert fresh is True  # idle (1 day threshold) trips
