"""Regression coverage for cron schedule edits and stepped day fields."""
import asyncio
from datetime import datetime, timedelta

import pytest


NOW = datetime(2026, 10, 3, 12)


def cron_stack(tmp_path, runner):
    from cron.jobs import CronJobStore
    from cron.scheduler import CronScheduler
    from cron.service import CronService
    store = CronJobStore(str(tmp_path / "cron.db"))
    service = CronService(store, now=lambda: NOW)
    scheduler = CronScheduler(store, runner, lock_path=str(tmp_path / "cron.lock"))
    return store, service, scheduler


def test_one_shot_changed_to_recurring_keeps_running(tmp_path):
    async def scenario():
        async def runner(job):
            return True
        store, service, scheduler = cron_stack(tmp_path, runner)
        job = service.schedule(task="research", user_id="u",
                               schedule_spec="2026-10-03T13:00:00")
        assert service.edit(job.id, user_id="u", schedule_spec="1h") == ["schedule"]
        await scheduler.tick(now=NOW + timedelta(hours=1))
        saved = store.get(job.id)
        assert (saved.one_shot, saved.status, saved.next_run_at) == (
            False, "scheduled", NOW + timedelta(hours=2))
    asyncio.run(scenario())


def test_recurring_changed_to_one_shot_failure_is_visible(tmp_path):
    async def scenario():
        async def runner(job):
            return False
        store, service, scheduler = cron_stack(tmp_path, runner)
        job = service.schedule(task="research", user_id="u", schedule_spec="1h")
        service.edit(job.id, user_id="u", schedule_spec="2026-10-03T13:00:00")
        await scheduler.tick(now=NOW + timedelta(hours=1))
        saved = store.get(job.id)
        assert (saved.one_shot, saved.status) == (True, "failed")
    asyncio.run(scenario())


def test_edit_of_waiting_due_job_takes_effect_before_it_starts(tmp_path):
    async def scenario():
        seen = []
        async def runner(job):
            seen.append((job.id, job.task))
            if job.id == "first":
                # A real owner/CLI edit can arrive while this runner awaits IO.
                service.edit("second", user_id="u", schedule_spec="1d",
                             old_text="OLD INSTRUCTIONS", new_text="NEW INSTRUCTIONS")
            return True
        store, service, scheduler = cron_stack(tmp_path, runner)
        service.schedule(job_id="first", task="first", user_id="u", schedule_spec="5m")
        service.schedule(job_id="second", task="OLD INSTRUCTIONS", user_id="u", schedule_spec="6m")
        await scheduler.tick(now=NOW + timedelta(minutes=10))
        assert seen == [("first", "first")], seen
    asyncio.run(scenario())


def test_running_job_completion_preserves_edited_next_run(tmp_path):
    async def scenario():
        async def runner(job):
            service.edit(job.id, user_id="u", schedule_spec="1d")
            assert store.get(job.id).next_run_at == NOW + timedelta(days=1)
            return True
        store, service, scheduler = cron_stack(tmp_path, runner)
        job = service.schedule(task="research", user_id="u", schedule_spec="5m")
        await scheduler.tick(now=NOW + timedelta(minutes=5))
        saved = store.get(job.id)
        assert (saved.schedule_spec, saved.next_run_at) == ("1d", NOW + timedelta(days=1))
    asyncio.run(scenario())


@pytest.mark.parametrize("spec,after,expected", [
    ("0 0 */2 * *", datetime(2026, 10, 1), datetime(2026, 10, 3)),
    ("0 0 * * */2", datetime(2026, 10, 4), datetime(2026, 10, 6)),
    ("0 0 */2 * 1", datetime(2026, 10, 1), datetime(2026, 10, 5)),
    ("0 0 5 * */2", datetime(2026, 10, 1), datetime(2026, 11, 5)),
])
def test_stepped_day_wildcards_filter_days(spec, after, expected):
    from core.schedule import parse_schedule
    assert parse_schedule(spec).next_run_after(after) == expected


def test_due_job_uses_current_task_and_cap(tmp_path):
    async def scenario():
        seen = []
        async def runner(job):
            if job.id == "first":
                service.edit("second", user_id="u", old_text="old", new_text="new",
                             max_duration_seconds=123)
            seen.append((job.id, job.task, job.max_duration_seconds))
            return True
        store, service, scheduler = cron_stack(tmp_path, runner)
        service.schedule(job_id="first", task="first", user_id="u", schedule_spec="5m")
        service.schedule(job_id="second", task="old", user_id="u", schedule_spec="6m")
        await scheduler.tick(now=NOW + timedelta(minutes=10))
        assert seen[-1] == ("second", "new", 123)
    asyncio.run(scenario())


def test_cancel_during_run_survives_completion(tmp_path):
    async def scenario():
        async def runner(job):
            service.cancel(job.id, user_id="u")
            return True
        store, service, scheduler = cron_stack(tmp_path, runner)
        job = service.schedule(task="research", user_id="u", schedule_spec="5m")
        await scheduler.tick(now=NOW + timedelta(minutes=5))
        assert store.get(job.id).status == "cancelled"
        assert not store.get(job.id).enabled
    asyncio.run(scenario())
