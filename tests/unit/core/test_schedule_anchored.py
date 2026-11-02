"""Anchored intervals, next fire times and the day-of-month step note (routines §2).

Why: a "every 3 days, judge 48 h after the announce" cycle was written as two
day-of-month crons (``0 18 */3 * *`` / ``0 18 2-31/3 * *``). Day-of-month
``*/3`` restarts each month, so the announce fired 10-31 AND 11-01 and one
round was never judged. ``every 3d from <ISO time>`` fires at anchor + k·3d.
"""
from datetime import datetime, timedelta

import pytest

from core.schedule import (ScheduleError, dom_step_note, next_runs, parse_schedule)


ANCHOR = datetime(2026, 10, 26, 6, 59)


def test_anchored_interval_fires_on_the_anchor_grid():
    s = parse_schedule("every 3d from 2026-10-26T06:59")
    assert s.kind == "interval" and not s.one_shot
    assert s.interval_seconds == 3 * 86400
    after = datetime(2026, 10, 29, 12, 0)
    assert s.next_run_after(after) == ANCHOR + timedelta(days=6)


def test_anchored_interval_does_not_reset_at_month_end():
    s = parse_schedule("every 3d from 2026-10-26T06:59")
    runs = next_runs(s, datetime(2026, 10, 29, 12, 0), 4)
    assert [r.day for r in runs] == [1, 4, 7, 10]
    assert [r.month for r in runs] == [11, 11, 11, 11]


def test_anchor_in_the_future_fires_first_at_the_anchor():
    s = parse_schedule("every 2h from 2026-11-01T09:00")
    assert s.next_run_after(datetime(2026, 10, 30, 0, 0)) == datetime(2026, 11, 1, 9, 0)


def test_run_exactly_at_a_slot_moves_to_the_next_slot():
    s = parse_schedule("every 3d from 2026-10-26T06:59")
    assert s.next_run_after(ANCHOR) == ANCHOR + timedelta(days=3)


def test_anchor_with_z_is_read_as_utc():
    s = parse_schedule("every 3d from 2026-10-26T06:59Z")
    assert s.anchor is not None and s.anchor.tzinfo is None


def test_plain_interval_has_no_anchor_and_keeps_its_behaviour():
    s = parse_schedule("every 2h")
    after = datetime(2026, 10, 29, 12, 7)
    assert s.anchor is None
    assert s.next_run_after(after) == after + timedelta(hours=2)


@pytest.mark.parametrize("spec", ["every 3d from yesterday", "every 3d from",
                                  "every banana from 2026-10-26T06:59"])
def test_bad_anchored_spec_is_refused(spec):
    with pytest.raises(ScheduleError):
        parse_schedule(spec)


def test_next_runs_for_a_one_shot_is_one_time():
    s = parse_schedule("2030-01-01T09:00:00")
    assert next_runs(s, datetime(2029, 1, 1), 3) == [datetime(2030, 1, 1, 9, 0)]


@pytest.mark.parametrize("spec", ["0 18 */3 * *", "0 18 2-31/3 * *"])
def test_dom_step_note_names_the_month_end_effect_and_the_anchored_form(spec):
    note = dom_step_note(spec)
    assert "month" in note and "every 3d from" in note


@pytest.mark.parametrize("spec", ["0 18 * * *", "*/15 * * * *", "0 9 1 * *", "3d",
                                  "every 3d from 2026-10-26T06:59", "not a spec"])
def test_dom_step_note_is_empty_for_other_specs(spec):
    assert dom_step_note(spec) == ""
