"""107 WS-2: a failing job stops and says so.

Before: a recurring job failed on every slot, at the cost of a model run each time, and
the owner never heard of a failure in a job the agent created.
"""
import asyncio
from datetime import datetime, timedelta

from core.cron_outcome import FAILURE_STREAK_LIMIT
from cron.delivery import failed_run_notice
from cron.jobs import CronJob, CronJobStore
from cron.scheduler import CronScheduler
from cron.service import next_run_text

NOW = datetime(2026, 11, 7, 12, 0)


def _setup(tmp_path, notes, payload=None, *, one_shot=False):
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(CronJob(id="job1", task="SCAN the feed", schedule_spec="1h", user_id="u1",
                      next_run_at=NOW, one_shot=one_shot, payload=payload or {}))
    seq = iter(notes)

    async def runner(job):
        note = next(seq)
        if note is None:
            return True
        job.run_note = note
        return False

    sch = CronScheduler(store, runner, lock_path=str(tmp_path / "lock"))
    lines, failures = [], []

    async def line(text):
        lines.append(text)

    async def failure(job, outcome, reason, at):
        failures.append(failed_run_notice(job, outcome, reason, at))

    sch.set_notice_hook(line)
    sch.set_failure_hook(failure)
    return store, sch, lines, failures


def _run(store, sch, times, start=NOW):
    at = start
    for _ in range(times):
        job = store.get("job1")
        if job.status != "scheduled":
            return
        at = max(at, job.next_run_at)
        asyncio.run(sch._run_due(at))


FAIL = {"outcome": "failed", "reason": "stopped at the step limit (150/150)"}


def test_three_failures_pause_the_job_and_tell_one_line(tmp_path):
    store, sch, lines, _ = _setup(tmp_path, [FAIL] * 5)
    _run(store, sch, 5)
    job = store.get("job1")
    assert job.status == "paused" and job.next_run_at is None
    assert job.payload["last_outcome"]["streak"] == FAILURE_STREAK_LIMIT
    assert len(lines) == 1
    assert "paused after 3 failed runs" in lines[0]
    assert "step limit" in lines[0] and "/job resume job1" in lines[0]
    assert next_run_text(job) == "paused after 3 failed runs"


def test_a_done_run_resets_the_count(tmp_path):
    store, sch, lines, _ = _setup(tmp_path, [FAIL, FAIL, None, FAIL, FAIL])
    _run(store, sch, 5)
    assert store.get("job1").status == "scheduled"
    assert store.get("job1").payload["last_outcome"]["streak"] == 2
    assert lines == []


def test_a_partial_run_counts(tmp_path):
    store, sch, lines, _ = _setup(tmp_path, [{"outcome": "partial", "reason": "cap"}] * 3)
    _run(store, sch, 3)
    assert store.get("job1").status == "paused"


def test_a_provider_outage_does_not_count(tmp_path):
    out = {"outcome": "failed", "reason": "the run was refused or produced nothing",
           "provider_down": True}
    store, sch, lines, _ = _setup(tmp_path, [out] * 4)
    _run(store, sch, 4)
    assert store.get("job1").status == "scheduled"
    assert lines == []
    assert "provider_down" not in store.get("job1").payload["last_outcome"]


def test_the_outage_words_are_read_from_the_status_text():
    from cron.scheduler import provider_down
    assert provider_down("Session failed: PERMANENT ERROR: 402 insufficient credits")
    assert provider_down("ALL LLM PROVIDERS EXHAUSTED")
    assert not provider_down("the tool refused the path")


def test_resume_clears_the_count(tmp_path):
    store, sch, _, _ = _setup(tmp_path, [FAIL] * 4)
    _run(store, sch, 3)
    assert store.resume("job1", user_id="u1", next_run_at=NOW + timedelta(days=1))
    job = store.get("job1")
    assert "streak" not in job.payload["last_outcome"] and "paused" not in job.payload
    _run(store, sch, 1, start=NOW + timedelta(days=1))
    assert store.get("job1").status == "scheduled"
    assert store.get("job1").payload["last_outcome"]["streak"] == 1


def test_the_digest_job_never_pauses(tmp_path):
    store, sch, lines, _ = _setup(tmp_path, [FAIL] * 4, payload={"digest": True})
    _run(store, sch, 4)
    assert store.get("job1").status == "scheduled" and lines == []


def test_an_agent_job_is_quiet_until_the_limit_and_then_tells(tmp_path):
    store, sch, lines, failures = _setup(tmp_path, [FAIL] * 3, payload={"armed_by": "agent"})
    _run(store, sch, 3)
    assert failures == [None, None]  # the third run trips the limit and sends its own line
    assert len(lines) == 1


def test_an_owner_job_tells_its_first_failure_and_then_the_limit(tmp_path):
    store, sch, lines, failures = _setup(tmp_path, [FAIL] * 3)
    _run(store, sch, 3)
    assert failures[0] and failures[1] is None
    assert len(lines) == 1


def test_a_failed_reminder_the_agent_set_tells_the_owner(tmp_path):
    store, sch, lines, failures = _setup(tmp_path, [FAIL], payload={"armed_by": "agent"},
                                         one_shot=True)
    _run(store, sch, 1)
    assert store.get("job1").status == "failed"
    assert failures and "did not finish" in failures[0]
    assert "/job run" not in failures[0]
    assert lines == []


def test_the_digest_lists_an_agent_job_that_the_limit_paused(tmp_path, monkeypatch):
    from cron import digest
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    store, sch, _, _ = _setup(tmp_path, [FAIL] * 3, payload={"armed_by": "agent"})
    assert digest._failed_jobs("u1", str(tmp_path)) == []
    _run(store, sch, 3)
    assert [j.id for j in digest._failed_jobs("u1", str(tmp_path))] == ["job1"]


def test_a_second_pause_with_the_same_reason_is_not_a_repeat_to_the_rail(tmp_path):
    from core.surfaces.user_delivery import content_hash
    store, sch, lines, _ = _setup(tmp_path, [FAIL] * 6)
    _run(store, sch, 3)
    assert store.resume("job1", user_id="u1", next_run_at=NOW + timedelta(days=1))
    _run(store, sch, 3, start=NOW + timedelta(days=1))
    assert len(lines) == 2 and content_hash(lines[0]) != content_hash(lines[1])


def test_a_skipped_tick_keeps_the_count(tmp_path):
    # A $0 skip (a quiet pre-check, an owner pause) did not do the job: it must not reset
    # the count, or a job that fails on every real run never reaches the limit.
    skip = {"outcome": "skipped", "reason": "the pre-check was quiet"}
    store, sch, lines, _ = _setup(tmp_path, [FAIL, skip, FAIL, skip, FAIL])

    async def runner(job, seq=iter([FAIL, skip, FAIL, skip, FAIL])):
        job.run_note = next(seq)
        return job.run_note["outcome"] == "skipped"
    sch.runner = runner
    _run(store, sch, 5)
    assert store.get("job1").status == "paused"
    assert len(lines) == 1


def test_the_first_failure_after_a_resume_is_told(tmp_path):
    # Before: the resume dropped the count but kept outcome "failed", so the first
    # failure of the new streak read as a repeat and the owner heard nothing.
    store, sch, _, failures = _setup(tmp_path, [FAIL] * 4)
    _run(store, sch, 3)
    assert store.resume("job1", user_id="u1", next_run_at=NOW + timedelta(days=1))
    _run(store, sch, 1, start=NOW + timedelta(days=1))
    assert failures[0] and failures[-1] and "did not finish" in failures[-1]


def test_a_skip_inside_a_streak_does_not_tell_the_streak_again(tmp_path):
    skip = {"outcome": "skipped", "reason": "late"}
    store, sch, _, failures = _setup(tmp_path, [])
    seq = iter([FAIL, skip, FAIL])

    async def runner(job):
        job.run_note = next(seq)
        return job.run_note["outcome"] == "skipped"
    sch.runner = runner
    _run(store, sch, 3)
    assert failures[0] and failures[-1] is None


def test_a_provider_outage_is_told_once(tmp_path):
    down = dict(FAIL, provider_down=True)
    store, sch, _, failures = _setup(tmp_path, [down] * 3)
    _run(store, sch, 3)
    assert failures[0] and failures[1:] == [None, None]
