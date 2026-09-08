"""Stream cadence vs. the hourly seeder timer (2026-08-29 "no goals" forensics, C1).

The seeder runs from an HOURLY systemd timer with ``RandomizedDelaySec=300``.
``stream_is_due`` compared ``now - last_seed`` against the cadence with a strict
``<``, so a tick landing seconds before the window closed ("4.0h of 4.0h") was
skipped and the stream waited a whole extra hour — a 4 h cadence ran at 5 h in
7 of 9 measured gaps on prod. A tolerance the size of the timer jitter fixes it.
"""
import time

import pytest

from agents.task.goals import streams as S
from agents.task.goals.board import GoalBoard


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _stream(cadence_hours=4):
    return {
        "id": "demo-stream",
        "cadence_hours": cadence_hours,
        "max_live_goals": 1,
        "objective": {"title": "Demo objective", "body": "Do the demo work."},
        "goals": [{"title": "step one", "body": "do one", "tools": ["task"]}],
    }


def _seed_and_finish(board, stream, seeded_at):
    """Seed one cycle at ``seeded_at`` and cancel it so nothing is live."""
    clock_board = GoalBoard(board.db_path, clock=lambda: seeded_at)
    oid, _ = S.ensure_objective(clock_board, "rob", stream)
    for g in S.seed_stream(clock_board, "rob", stream, oid):
        clock_board.cancel(g.id, user_id="rob")


def test_a_tick_just_inside_the_window_counts_as_due(board):
    """A 4 h stream seeded 3 h 52 min ago is due — the timer jitter must not
    cost the stream a whole extra hour."""
    stream = _stream(4)
    now = time.time()
    _seed_and_finish(board, stream, now - (4 * 3600 - 8 * 60))
    due, why = S.stream_is_due(board, "rob", stream, now)
    assert due is True, why


def test_a_tick_well_inside_the_window_is_still_skipped(board):
    stream = _stream(4)
    now = time.time()
    _seed_and_finish(board, stream, now - 2 * 3600)
    due, why = S.stream_is_due(board, "rob", stream, now)
    assert due is False and "cadence" in why


def test_tolerance_never_exceeds_a_quarter_of_a_short_cadence(board):
    """A 1 h cadence must not become a 45 min one: tolerance is capped."""
    stream = _stream(1)
    now = time.time()
    _seed_and_finish(board, stream, now - 40 * 60)
    due, _ = S.stream_is_due(board, "rob", stream, now)
    assert due is False


def test_next_seed_at_reports_when_the_earliest_idle_stream_reopens(board):
    stream = _stream(4)
    now = time.time()
    seeded = now - 1 * 3600
    _seed_and_finish(board, stream, seeded)
    nxt = S.next_seed_at(board, "rob", [stream], now)
    assert nxt is not None
    # window closes at seeded + 4h minus the tolerance
    assert abs(nxt - (seeded + 4 * 3600 - S.CADENCE_TOLERANCE_SEC)) < 2


def test_next_seed_at_is_none_when_a_stream_is_due_now(board):
    stream = _stream(4)
    now = time.time()
    _seed_and_finish(board, stream, now - 6 * 3600)
    assert S.next_seed_at(board, "rob", [stream], now) is None


def test_next_seed_at_is_none_for_a_never_seeded_stream(board):
    assert S.next_seed_at(board, "rob", [_stream(4)], time.time()) is None


def test_next_seed_at_skips_a_stream_held_by_live_goals(board):
    """A stream at its live ceiling has no next seed time to report — it is
    working, not idle."""
    stream = _stream(4)
    oid, _ = S.ensure_objective(board, "rob", stream)
    S.seed_stream(board, "rob", stream, oid)  # left live
    assert S.next_seed_at(board, "rob", [stream], time.time()) is None


def test_stream_overdue_by_is_zero_inside_the_window_and_grows_past_it(board):
    stream = _stream(4)
    now = time.time()
    _seed_and_finish(board, stream, now - 2 * 3600)
    assert S.stream_overdue_by(board, "rob", stream, now) == 0.0
    late = now + 4 * 3600
    overdue = S.stream_overdue_by(board, "rob", stream, late)
    # seeded at now-2h, window = 4h - tolerance -> overdue by 2h + tolerance
    assert abs(overdue - (2 * 3600 + S.CADENCE_TOLERANCE_SEC)) < 2


def test_stream_overdue_by_is_zero_for_a_busy_or_unseeded_stream(board):
    stream = _stream(4)
    assert S.stream_overdue_by(board, "rob", stream, time.time()) == 0.0
    oid, _ = S.ensure_objective(board, "rob", stream)
    S.seed_stream(board, "rob", stream, oid)  # live -> busy
    assert S.stream_overdue_by(board, "rob", stream, time.time() + 9 * 3600) == 0.0
