"""080 WS-2: a job's own owner state — pause / resume / run now / stop with an
actor, the last outcome on the row, and the ONE scheduler-off sentence."""
from datetime import datetime

import pytest

from cron.jobs import CronJobStore
from cron.schedule import ScheduleError
from cron.service import (CronService, last_outcome, next_run_text,
                          parse_new_job_args, scheduler_off_note)

NOW = datetime.fromisoformat("2026-10-13T12:00:00")


def _svc(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    return CronService(store, now=lambda: NOW), store


def test_pause_is_the_jobs_own_state_and_hides_the_next_run(tmp_path):
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    assert svc.pause(job.id, user_id="u1", via="telegram") is True
    row = store.get(job.id)
    assert row.status == "paused" and row.next_run_at is None
    assert row.payload["paused"]["via"] == "telegram"
    assert next_run_text(row) == "paused"
    # The ticker never sees it.
    assert store.due(datetime.fromisoformat("2030-01-01T00:00:00")) == []
    # A second pause is not a state change.
    assert svc.pause(job.id, user_id="u1", via="telegram") is False


def test_resume_recomputes_the_next_run_and_clears_the_pause(tmp_path):
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    svc.pause(job.id, user_id="u1", via="cli")
    assert svc.resume(job.id, user_id="u1", via="cli") is True
    row = store.get(job.id)
    assert row.status == "scheduled" and row.next_run_at == datetime(2026, 10, 13, 13, 0)
    assert "paused" not in row.payload
    assert svc.resume(job.id, user_id="u1", via="cli") is False


def test_pause_is_tenant_scoped(tmp_path):
    svc, _ = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    assert svc.pause(job.id, user_id="u2", via="cli") is False


def test_scheduler_status_write_never_lifts_an_owner_pause(tmp_path):
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    svc.pause(job.id, user_id="u1", via="cli")
    store.set_status(job.id, "scheduled")
    assert store.get(job.id).status == "paused"


def test_edit_schedule_of_a_paused_job_keeps_no_next_run(tmp_path):
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    svc.pause(job.id, user_id="u1", via="cli")
    assert svc.edit(job.id, user_id="u1", via="cli", schedule_spec="2h") == ["schedule"]
    row = store.get(job.id)
    assert row.schedule_spec == "2h" and row.next_run_at is None and row.status == "paused"


def test_run_now_pulls_a_scheduled_job_and_names_why_not(tmp_path):
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    assert svc.run_now(job.id, user_id="u1", via="repl") == ""
    assert store.get(job.id).next_run_at == NOW
    svc.pause(job.id, user_id="u1", via="repl")
    assert svc.run_now(job.id, user_id="u1", via="repl") == "paused"
    assert svc.run_now("nope", user_id="u1", via="repl") == "missing"


def test_stop_requires_an_actor_and_records_it_on_the_row(tmp_path):
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    with pytest.raises(ValueError):
        svc.cancel(job.id, user_id="u1", via="")
    with pytest.raises(TypeError):
        svc.cancel(job.id, user_id="u1")  # no default actor
    assert svc.cancel(job.id, user_id="u1", via="telegram") is True
    row = store.get(job.id)
    assert row.status == "cancelled"
    assert row.payload["stopped"]["via"] == "telegram"
    assert row.payload["stopped"]["by"] == "u1"


def test_every_owner_operation_requires_an_actor(tmp_path):
    svc, _ = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    for op in (svc.pause, svc.resume, svc.run_now):
        with pytest.raises(ValueError):
            op(job.id, user_id="u1", via="  ")


def test_last_outcome_lives_on_the_row(tmp_path):
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    assert last_outcome(store.get(job.id)) == "never run"
    store.record_outcome(job.id, "failed", NOW)
    assert last_outcome(store.get(job.id)) == "failed 10-13 12:00"


def test_scheduler_records_the_outcome(tmp_path):
    from cron.scheduler import CronScheduler
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    sched = object.__new__(CronScheduler)
    sched.store = store
    sched._answered_during_run = lambda *a, **k: False
    sched._record(store.get(job.id), NOW, False)
    assert store.get(job.id).payload["last_outcome"]["outcome"] == "failed"


def test_paused_job_counts_toward_the_duplicate_check(tmp_path):
    svc, _ = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    svc.pause(job.id, user_id="u1", via="cli")
    assert svc.find_duplicate(user_id="u1", task=" digest ", schedule_spec="1h").id == job.id
    assert svc.find_duplicate(user_id="u1", task="digest", schedule_spec="2h") is None


@pytest.mark.parametrize("tokens,want", [
    (["every", "day", "08:00", "|", "Summarize", "news"], ("every day 08:00", "Summarize news")),
    (["'every", "day", "08:00'", "Summarize", "news"], ("every day 08:00", "Summarize news")),
    (["every day 08:00", "Summarize", "news"], ("every day 08:00", "Summarize news")),
    (["every", "day", "08:00", "Summarize", "news"], ("every day 08:00", "Summarize news")),
    (["30m", "check", "the", "inbox"], ("30m", "check the inbox")),
    (["0", "9", "*", "*", "*", "post", "it"], ("0 9 * * *", "post it")),
    (["every", "day", "08:00", "don't", "stop"], ("every day 08:00", "don't stop")),
])
def test_one_new_job_grammar(tokens, want):
    assert parse_new_job_args(tokens) == want


def test_new_job_grammar_refuses_without_a_schedule():
    with pytest.raises(ScheduleError):
        parse_new_job_args(["Summarize", "news"])
    with pytest.raises(ScheduleError):
        parse_new_job_args(["30m"])


def test_scheduler_off_note_is_one_sentence_with_one_remedy(monkeypatch):
    monkeypatch.setattr("tools.cronjob_tools.cron_enabled", lambda: False)
    note = scheduler_off_note()
    assert "scheduler is off" in note and "nothing will run" in note
    assert "AUTONOMY_POSTURE full" in note and " or " not in note
    assert "CRON_ENABLED" not in note  # a member line is ignored; never the remedy
    monkeypatch.setattr("tools.cronjob_tools.cron_enabled", lambda: True)
    assert scheduler_off_note() == ""


def test_scheduler_off_note_says_when_it_could_not_check(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr("tools.cronjob_tools.cron_enabled", boom)
    assert "could not check" in scheduler_off_note()
