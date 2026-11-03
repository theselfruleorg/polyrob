"""098 MO-12: the streak ask latch resets when its ask closes; one ask per 7 days."""
import time
import types

import pytest

from agents.task.goals.board import GoalBoard
from core.goal_vocab import ASK_OPEN
from cron import remedy_streak as rs
from cron.jobs import CronJob, CronJobStore

NEED = "owner must log in again"


def _job(job_id="job1"):
    return types.SimpleNamespace(id=job_id, user_id="alice", task="X OUTREACH")


@pytest.fixture
def home(tmp_path):
    return str(tmp_path)


def _board(home):
    return GoalBoard(f"{home}/goals.db")


def _streak(home, job, n):
    out = None
    for _ in range(n):
        out = rs.observe(job, NEED, data_dir=home) or out
    return out


def _age_ask(home, job_id, days):
    from core.sqlite_util import execute_retry
    execute_retry(f"{home}/cron.db", "UPDATE remedy_streak SET asked_at=? WHERE job_id=?",
                  (time.time() - days * 86400, job_id))


def test_answered_ask_allows_new_streak_after_3_runs(home):
    job = _job()
    ask = _streak(home, job, 3)
    assert ask is not None
    _board(home).decide_ask(ask.id, user_id="alice", approved=True, answer="A")
    _age_ask(home, job.id, 8)
    assert _streak(home, job, 2) is None          # a new streak needs 3 runs again
    second = rs.observe(job, NEED, data_dir=home)
    assert second is not None and second.id != ask.id


def test_second_ask_rate_limited_to_7_days(home):
    job = _job()
    ask = _streak(home, job, 3)
    _board(home).decide_ask(ask.id, user_id="alice", approved=True, answer="A")
    assert _streak(home, job, 5) is None          # answered 1 day ago: no second ask
    assert len(_board(home).asks(user_id="alice", status=ASK_OPEN)) == 0


def test_open_ask_still_asks_once(home):
    job = _job()
    assert _streak(home, job, 3) is not None
    assert _streak(home, job, 5) is None
    assert len(_board(home).asks(user_id="alice", status=ASK_OPEN)) == 1


def test_cancel_deletes_streak_row(home):
    from datetime import datetime
    store = CronJobStore(f"{home}/cron.db")
    store.add(CronJob(id="job1", task="t", schedule_spec="1h", user_id="alice",
                      next_run_at=datetime(2026, 1, 1)))
    _streak(home, _job(), 2)
    assert rs._Store(f"{home}/cron.db").get("job1") is not None
    assert store.cancel("job1")
    assert rs._Store(f"{home}/cron.db").get("job1") is None
