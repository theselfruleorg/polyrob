"""Regression: 5-field cron DOM/DOW must use POSIX OR when both are restricted.

`0 9 13 * 5` means "09:00 on the 13th, PLUS every Friday" (union), not "only a
Friday that is also the 13th" (which the previous AND implementation produced).
"""
from datetime import datetime, timedelta

from cron.schedule import parse_schedule


def _fire_days(spec, year, month):
    sched = parse_schedule(spec)
    start = datetime(year, month, 1) - timedelta(minutes=1)
    end = datetime(year, month, 28)  # stay within every month
    days = set()
    cur = start
    while True:
        nxt = sched.next_run_after(cur)
        if nxt is None or nxt >= end:
            break
        if nxt.month == month:
            days.add(nxt.day)
        cur = nxt
    return days


def _fridays(year, month):
    return {d for d in range(1, 29)
            if datetime(year, month, d).isoweekday() == 5}


def test_both_restricted_is_union():
    # 09:00 on the 13th OR every Friday.
    days = _fire_days("0 9 13 * 5", 2026, 1)
    expected = _fridays(2026, 1) | {13}
    assert days == expected
    # 13 is present even though 2026-01-13 is NOT a Friday.
    assert datetime(2026, 1, 13).isoweekday() != 5
    assert 13 in days


def test_dom_only_restricted_is_and_semantics():
    # Only the 13th (dow is '*').
    days = _fire_days("0 9 13 * *", 2026, 1)
    assert days == {13}


def test_dow_only_restricted_fires_all_fridays():
    days = _fire_days("0 9 * * 5", 2026, 1)
    assert days == _fridays(2026, 1)


def test_wildcards_unaffected():
    # */15 dom/dow are wildcards -> every day matches (spot-check a few days).
    days = _fire_days("0 9 * * *", 2026, 1)
    assert {1, 2, 15, 27}.issubset(days)
