"""P5a — schedule parsing + next-run computation (pure, tz-naive local)."""
from datetime import datetime

import pytest

from cron.schedule import parse_schedule, Schedule, ScheduleError


def _dt(s):
    return datetime.fromisoformat(s)


# --- duration / interval -----------------------------------------------------

@pytest.mark.parametrize("spec,minutes", [("30m", 30), ("2h", 120), ("1d", 1440), ("45s", 0.75)])
def test_duration_interval_next_run(spec, minutes):
    sch = parse_schedule(spec)
    assert sch.kind == "interval" and sch.one_shot is False
    after = _dt("2026-06-06T12:00:00")
    nxt = sch.next_run_after(after)
    assert (nxt - after).total_seconds() == pytest.approx(minutes * 60)


def test_every_n_unit_phrase_is_interval():
    sch = parse_schedule("every 15m")
    assert sch.kind == "interval"
    after = _dt("2026-06-06T12:00:00")
    assert (sch.next_run_after(after) - after).total_seconds() == 15 * 60


# --- one-shot ISO timestamp --------------------------------------------------

def test_iso_timestamp_is_one_shot_future():
    sch = parse_schedule("2026-06-07T09:30:00")
    assert sch.one_shot is True and sch.kind == "once"
    assert sch.next_run_after(_dt("2026-06-06T12:00:00")) == _dt("2026-06-07T09:30:00")


def test_iso_timestamp_past_returns_none():
    sch = parse_schedule("2026-06-01T09:30:00")
    assert sch.next_run_after(_dt("2026-06-06T12:00:00")) is None


# --- 5-field cron ------------------------------------------------------------

def test_cron_every_15_min():
    sch = parse_schedule("*/15 * * * *")
    assert sch.kind == "cron" and sch.one_shot is False
    nxt = sch.next_run_after(_dt("2026-06-06T12:07:00"))
    assert nxt == _dt("2026-06-06T12:15:00")


def test_cron_daily_at_time():
    sch = parse_schedule("30 9 * * *")
    nxt = sch.next_run_after(_dt("2026-06-06T12:00:00"))
    assert nxt == _dt("2026-06-07T09:30:00")  # next day 09:30


def test_cron_weekday_monday_9am():
    # 2026-06-06 is a Saturday; next Monday is 2026-06-08
    sch = parse_schedule("0 9 * * 1")  # dow 1 = Monday
    nxt = sch.next_run_after(_dt("2026-06-06T12:00:00"))
    assert nxt == _dt("2026-06-08T09:00:00")


def test_cron_sunday_zero_and_seven_equivalent():
    a = parse_schedule("0 0 * * 0").next_run_after(_dt("2026-06-06T12:00:00"))
    b = parse_schedule("0 0 * * 7").next_run_after(_dt("2026-06-06T12:00:00"))
    assert a == b == _dt("2026-06-07T00:00:00")  # next Sunday midnight


# --- every-phrase weekday / daily --------------------------------------------

def test_every_day_at_time():
    sch = parse_schedule("every day 14:30")
    nxt = sch.next_run_after(_dt("2026-06-06T12:00:00"))
    assert nxt == _dt("2026-06-06T14:30:00")


def test_every_weekday_phrase():
    sch = parse_schedule("every monday 09:00")
    nxt = sch.next_run_after(_dt("2026-06-06T12:00:00"))
    assert nxt == _dt("2026-06-08T09:00:00")


# --- errors ------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "garbage", "99x", "* * *", "every flooberday 9am"])
def test_bad_spec_raises(bad):
    with pytest.raises(ScheduleError):
        parse_schedule(bad)


def test_iso_timestamp_tz_aware_does_not_crash():
    # A tz-aware one-shot must normalize to naive local so next_run_after (which
    # compares against a naive datetime.now()) doesn't raise a naive/aware TypeError.
    from datetime import datetime, timedelta
    sch = parse_schedule("2999-01-01T00:00:00+02:00")
    assert sch.kind == "once" and sch.once_at.tzinfo is None
    # Comparison against a naive datetime must work.
    nxt = sch.next_run_after(datetime.now())
    assert nxt is not None


def test_every_zero_interval_rejected():
    with pytest.raises(ScheduleError):
        parse_schedule("every 0m")
    with pytest.raises(ScheduleError):
        parse_schedule("every 0s")
