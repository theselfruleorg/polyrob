"""P5d — CronService: schedule/list/cancel over the store."""
from datetime import datetime

import pytest

from cron.jobs import CronJobStore
from cron.schedule import ScheduleError
from cron.service import CronService


def _svc(tmp_path, now="2026-06-06T12:00:00"):
    store = CronJobStore(str(tmp_path / "cron.db"))
    return CronService(store, now=lambda: datetime.fromisoformat(now)), store


def test_schedule_recurring_computes_next_run(tmp_path):
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="ping the api", schedule_spec="*/15 * * * *", user_id="u1")
    assert job.one_shot is False
    assert job.next_run_at == datetime(2026, 6, 6, 12, 15, 0)
    assert store.get(job.id) is not None


def test_schedule_one_shot_iso(tmp_path):
    svc, _ = _svc(tmp_path)
    job = svc.schedule(task="do once", schedule_spec="2026-06-07T09:00:00", user_id="u1")
    assert job.one_shot is True
    assert job.next_run_at == datetime(2026, 6, 7, 9, 0, 0)


def test_schedule_defaults_max_duration_cap(tmp_path):
    svc, _ = _svc(tmp_path)
    job = svc.schedule(task="t", schedule_spec="1h", user_id="u1")
    assert job.max_duration_seconds == 600  # 10-minute hard cap default
    # skip_memory is a DORMANT column (ME-D2) — schedule() no longer accepts it,
    # and the job still takes the dataclass/schema default.
    assert job.skip_memory is True


def test_schedule_past_one_shot_raises(tmp_path):
    svc, _ = _svc(tmp_path)
    with pytest.raises(ScheduleError):
        svc.schedule(task="t", schedule_spec="2026-06-01T09:00:00", user_id="u1")


def test_schedule_bad_spec_raises(tmp_path):
    svc, _ = _svc(tmp_path)
    with pytest.raises(ScheduleError):
        svc.schedule(task="t", schedule_spec="not-a-schedule", user_id="u1")


def test_list_and_cancel(tmp_path):
    svc, _ = _svc(tmp_path)
    j = svc.schedule(task="t", schedule_spec="1h", user_id="u1")
    svc.schedule(task="t2", schedule_spec="1h", user_id="u2")
    assert {x.id for x in svc.list_jobs(user_id="u1")} == {j.id}
    assert svc.cancel(j.id, via="cli") is True
    assert svc.cancel("missing", via="cli") is False


def test_payload_round_trips(tmp_path):
    svc, store = _svc(tmp_path)
    j = svc.schedule(task="t", schedule_spec="1h", user_id="u1",
                     payload={"provider": "anthropic", "model": "claude-opus-4-8"})
    assert store.get(j.id).payload["provider"] == "anthropic"


def test_a_pause_reply_names_a_cut_run_that_the_pause_drops(tmp_path):
    """A pause drops an unsettled cut mark, so the pause reply is the only place the
    owner hears that a restart cut a run which cannot repeat by itself."""
    svc, store = _svc(tmp_path)
    model_job = svc.schedule(task="post the daily note", schedule_spec="*/15 * * * *", user_id="u1")
    read_job = svc.schedule(task="read", schedule_spec="*/15 * * * *", user_id="u1",
                            payload={"wake_agent": False})
    assert svc.cut_note(model_job.id, user_id="u1") == ""
    store.mark_cut_run(model_job.id)
    store.mark_cut_run(read_job.id)
    assert "cut by a restart" in svc.cut_note(model_job.id, user_id="u1")
    assert svc.cut_note(read_job.id, user_id="u1") == ""
    assert svc.cut_note(model_job.id, user_id="other") == ""
    store.mark_cut_run(model_job.id, by="pause")
    assert "cut by an owner pause" in svc.cut_note(model_job.id, user_id="u1")
    assert svc.pause(model_job.id, user_id="u1", via="cli")
    assert svc.cut_note(model_job.id, user_id="u1") == ""
