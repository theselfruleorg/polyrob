"""A cron rail that reports the same owner-only remedy run after run asks ONCE.

Live case (intel 2026-09-28): X OUTREACH logged "Phase 1 BLOCKED (owner remedy:
`polyrob x-account capture-session`)" on 11 daily runs and nobody was asked.
"""
import types

import pytest

from agents.task.goals.board import GoalBoard
from core.goal_vocab import ASK_OPEN
from cron import remedy_streak as rs

BLOCKED = ("Collected 20 targets. Phase 1 BLOCKED (owner remedy: "
           "`polyrob x-account capture-session`). No owner ping per STEP 6.")


def _job(job_id="job1", user="rob"):
    return types.SimpleNamespace(id=job_id, user_id=user,
                                 task="X OUTREACH — daily round")


@pytest.fixture
def home(tmp_path):
    return str(tmp_path)


def _open_asks(home, user="rob"):
    return GoalBoard(f"{home}/goals.db").asks(user_id=user, status=ASK_OPEN)


def test_remedy_needs_a_block_marker_and_a_cli_call():
    assert rs.remedy_in(BLOCKED) == "polyrob x-account capture-session"
    # a CLI call with no block marker is a report, not a blocker
    assert rs.remedy_in("Ran `polyrob wallet balance`: 0.3 ETH") is None
    # a block with no owner command names no remedy
    assert rs.remedy_in("Phase 1 BLOCKED by rate limit") is None
    assert rs.remedy_in("") is None and rs.remedy_in(None) is None


def test_third_identical_block_raises_one_ask(home):
    job = _job()
    assert rs.observe(job, BLOCKED, data_dir=home) is None
    assert rs.observe(job, BLOCKED, data_dir=home) is None
    ask = rs.observe(job, BLOCKED, data_dir=home)
    assert ask is not None
    asks = _open_asks(home)
    assert [a.id for a in asks] == [ask.id]
    p = asks[0].payload
    assert p["rail_id"] == "cron:job1"
    assert p["remedy"] == "polyrob x-account capture-session"
    assert set(p["options"]) == {"A", "B"}
    assert "polyrob x-account capture-session" in asks[0].title


def test_streak_keeps_one_ask_while_it_continues(home):
    job = _job()
    for _ in range(3):
        rs.observe(job, BLOCKED, data_dir=home)
    for _ in range(4):
        assert rs.observe(job, BLOCKED, data_dir=home) is None
    assert len(_open_asks(home)) == 1


def test_a_clean_run_resets_the_streak(home):
    job = _job()
    rs.observe(job, BLOCKED, data_dir=home)
    rs.observe(job, BLOCKED, data_dir=home)
    rs.observe(job, "Phase 1 done: 5 replies posted.", data_dir=home)
    assert rs.observe(job, BLOCKED, data_dir=home) is None
    assert rs.observe(job, BLOCKED, data_dir=home) is None
    assert _open_asks(home) == []


def test_a_different_remedy_restarts_the_count(home):
    job = _job()
    rs.observe(job, BLOCKED, data_dir=home)
    rs.observe(job, BLOCKED, data_dir=home)
    other = "BLOCKED (owner remedy: `polyrob wallet pin-token`)"
    assert rs.observe(job, other, data_dir=home) is None
    assert _open_asks(home) == []


def test_streaks_are_per_job(home):
    a, b = _job("a"), _job("b")
    rs.observe(a, BLOCKED, data_dir=home)
    rs.observe(b, BLOCKED, data_dir=home)
    rs.observe(a, BLOCKED, data_dir=home)
    assert _open_asks(home) == []


def test_after_the_ask_is_answered_a_new_streak_asks_again(home):
    job = _job()
    for _ in range(3):
        ask = rs.observe(job, BLOCKED, data_dir=home)
    GoalBoard(f"{home}/goals.db").fulfill_ask(ask.id, user_id="rob")
    assert _open_asks(home) == []
    rs.observe(job, "clean run", data_dir=home)
    for _ in range(2):
        assert rs.observe(job, BLOCKED, data_dir=home) is None
    assert rs.observe(job, BLOCKED, data_dir=home) is not None


def test_fail_open_on_an_unwritable_home(tmp_path):
    f = tmp_path / "not-a-dir"
    f.write_text("x")
    assert rs.observe(_job(), BLOCKED, data_dir=str(f)) is None
