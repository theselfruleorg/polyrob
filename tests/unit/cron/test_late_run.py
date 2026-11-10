"""107 WS-4: a late run says that it is late; a money job too far past its slot stays out."""
import asyncio
from datetime import datetime, timedelta

import pytest

from core.cron_outcome import late_task_line, outcome_text, span_text, waiting_text
from cron.jobs import CronJob, CronJobStore
from cron.scheduler import CronScheduler
from cron.service import next_run_text

NOW = datetime(2026, 11, 7, 12, 0)


@pytest.fixture
def rig(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    ran, lines = [], []

    async def runner(job):
        ran.append(job)
        return True

    sch = CronScheduler(store, runner, lock_path=str(tmp_path / "lock"))

    async def line(text):
        lines.append(text)

    sch.set_notice_hook(line)
    return store, sch, ran, lines


def _add(store, *, late_minutes, payload=None, one_shot=False):
    store.add(CronJob(id="job1", task="BUY the dip", schedule_spec="1h", user_id="u1",
                      next_run_at=NOW - timedelta(minutes=late_minutes), one_shot=one_shot,
                      payload=payload or {}))


def test_the_span_words():
    assert span_text(40 * 60) == "40m"
    assert span_text(3 * 3600) == "3h"
    assert span_text(3 * 86400) == "3d"


def test_waiting_text_starts_at_the_limit():
    assert waiting_text(NOW - timedelta(minutes=29), NOW) == ""
    assert waiting_text(NOW - timedelta(minutes=40), NOW) == "waiting 40m"
    assert waiting_text(None, NOW) == ""


def test_the_view_says_how_long_a_job_waited():
    job = CronJob(id="j", task="t", schedule_spec="1h", user_id="u",
                  next_run_at=datetime.now() - timedelta(minutes=40))
    assert next_run_text(job).endswith(", waiting 40m")
    job.next_run_at = datetime.now() + timedelta(minutes=40)
    assert "waiting" not in next_run_text(job)


def test_a_late_run_is_marked_and_recorded(rig):
    store, sch, ran, _ = rig
    _add(store, late_minutes=120)
    asyncio.run(sch._run_due(NOW))
    assert ran[0].late_seconds == 7200
    rec = store.get("job1").payload["last_outcome"]
    assert rec["late_by"] == 7200
    assert outcome_text(rec).endswith(", 2h late")


def test_an_on_time_run_is_not_marked(rig):
    store, sch, ran, _ = rig
    _add(store, late_minutes=5)
    asyncio.run(sch._run_due(NOW))
    assert ran[0].late_seconds == 0
    assert "late_by" not in store.get("job1").payload["last_outcome"]


def test_the_task_line_names_both_times():
    line = late_task_line(7200, NOW)
    assert "2h late" in line and "11-07 10:00" in line and "11-07 12:00" in line


MONEY = {"priority": "money"}


def test_a_money_job_past_the_limit_gives_up_its_slot(rig):
    store, sch, ran, lines = rig
    _add(store, late_minutes=61, payload=MONEY)
    result = asyncio.run(sch._run_due(NOW))
    job = store.get("job1")
    assert ran == [] and result.deferred == ["job1"]
    assert job.status == "scheduled" and job.next_run_at == NOW + timedelta(hours=1)
    assert job.last_run_at is None
    assert job.payload["last_outcome"]["outcome"] == "skipped"
    assert len(lines) == 1 and "61m late" in lines[0] and "/job run job1" in lines[0]


def test_a_money_job_inside_the_limit_runs(rig):
    store, sch, ran, lines = rig
    _add(store, late_minutes=59, payload=MONEY)
    asyncio.run(sch._run_due(NOW))
    assert len(ran) == 1 and lines == []


def test_a_late_one_time_money_job_ends_as_failed(rig):
    store, sch, ran, lines = rig
    _add(store, late_minutes=61, payload=MONEY, one_shot=True)
    asyncio.run(sch._run_due(NOW))
    assert ran == [] and store.get("job1").status == "failed"
    assert "will not run again" in lines[0]


def test_other_late_jobs_still_run(rig):
    store, sch, ran, lines = rig
    _add(store, late_minutes=600)
    asyncio.run(sch._run_due(NOW))
    assert len(ran) == 1 and lines == []


def test_a_run_that_waits_on_an_owner_answer_is_not_skipped(rig):
    store, sch, ran, _ = rig
    _add(store, late_minutes=61, payload={**MONEY, "last_outcome": {"outcome": "deferred"}})
    asyncio.run(sch._run_due(NOW))
    assert len(ran) == 1


def test_an_owner_pause_is_not_lateness(rig, monkeypatch):
    from types import SimpleNamespace
    import core.autonomy_control as ac
    monkeypatch.setattr(ac, "allows", lambda kind: SimpleNamespace(allowed=False, reason="pause"))
    store, sch, ran, lines = rig
    _add(store, late_minutes=61, payload=MONEY)
    asyncio.run(sch._run_due(NOW))
    assert len(ran) == 1 and lines == []  # the runner's own pause check handles it


def test_a_read_with_no_model_turn_is_not_skipped(rig):
    store, sch, ran, lines = rig
    _add(store, late_minutes=120, payload={**MONEY, "read_verb": "defi_data.x"})
    asyncio.run(sch._run_due(NOW))
    assert len(ran) == 1 and lines == []


def test_a_job_that_gives_up_its_slot_does_not_cancel_a_goal(rig, monkeypatch):
    from core.config_policy import AutonomyConfig
    store, sch, _, _ = rig
    monkeypatch.setattr(AutonomyConfig, "goal_yield_for_money_rail", staticmethod(lambda: True))
    yielded = []

    async def hook(job):
        yielded.append(job.id)
        return ["g1"]

    sch.set_yield_hook(hook)
    _add(store, late_minutes=120, payload=MONEY)
    assert asyncio.run(sch._maybe_yield_for_money(NOW)) == [] and yielded == []


@pytest.mark.parametrize("payload", [
    {**MONEY, "position": {"chain": "base", "address": "0xabc"}},
    {**MONEY, "mandate_job": "close_all"},
    {**MONEY, "skills": ["exits", "trade-execution"]},
])
def test_a_late_exit_job_still_runs(rig, payload):
    store, sch, ran, lines = rig
    _add(store, late_minutes=600, payload=payload)
    asyncio.run(sch._run_due(NOW))
    assert len(ran) == 1 and lines == []


def test_a_late_buy_job_is_still_skipped(rig):
    store, sch, ran, _ = rig
    _add(store, late_minutes=61, payload={**MONEY, "skills": ["dca", "trade-execution"]})
    asyncio.run(sch._run_due(NOW))
    assert ran == []


def test_the_note_for_the_console_and_every_view():
    from cron.service import next_note
    job = CronJob(id="j", task="t", schedule_spec="1h", user_id="u",
                  next_run_at=datetime.now() - timedelta(minutes=40))
    assert next_note(job) == "waiting 40m"
    job.status, job.next_run_at, job.payload = "paused", None, {"paused": {"by": "failure_streak"}}
    assert next_note(job) == "paused after 3 failed runs"
    job.payload = {"paused": {"by": "owner"}}
    assert next_note(job) == "" and next_run_text(job) == "paused"


def test_a_stored_time_that_cannot_be_read_has_no_wait():
    assert waiting_text("not a time") == ""
    assert waiting_text(None) == ""


def test_an_aware_rearm_is_stored_as_naive_local_time(tmp_path):
    from datetime import timezone
    from core.cron_rearm import rearm_job
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(CronJob(id="job1", task="t", schedule_spec="1h", user_id="u1",
                      next_run_at=NOW + timedelta(hours=1)))
    assert rearm_job(store.db_path, "job1", datetime.now(timezone.utc))
    assert store.get("job1").next_run_at.tzinfo is None


def test_an_aware_slot_already_stored_runs_and_does_not_crash(rig):
    from datetime import timezone
    store, sch, ran, _ = rig
    _add(store, late_minutes=0)
    aware = (NOW - timedelta(minutes=5)).astimezone(timezone.utc)
    from core.sqlite_util import execute_retry
    execute_retry(store.db_path, "UPDATE cron_jobs SET next_run_at=? WHERE id=?",
                  (aware.isoformat(), "job1"))
    asyncio.run(sch._run_due(NOW))
    assert [j.id for j in ran] == ["job1"]
    assert store.get("job1").status == "scheduled"
