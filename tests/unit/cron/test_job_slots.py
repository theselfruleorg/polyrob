"""A job the agent writes goes to a free slot, so the owner does not ask for the move.

The tick runs due jobs one at a time. Two model runs in one slot queue, and the note
the agent saw after the write did not make it fix the time.
"""
import types
from datetime import datetime, timedelta

import pytest

import tools.goal_tools as gt
from core.schedule import parse_schedule, shift_spec
from cron.jobs import CronJobStore
from cron.service import CronService
from core.cron_slots import SlotTable, holds_a_slot, place
from tools.cronjob_tools import (CronEditAction, CronJobTool, CronListAction,
                                 CronScheduleAction)

NOW = datetime(2026, 11, 9, 7, 0)


def _tool(tmp_path):
    t = object.__new__(CronJobTool)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def _ctx(user="u1"):
    return types.SimpleNamespace(user_id=user, role="orchestrator", is_sub_agent=False,
                                 metadata={}, session_id=None)


@pytest.fixture(autouse=True)
def owner_turn(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    monkeypatch.setattr(gt, "owner_seat_turn", lambda ctx: True)


def _sched(task, spec, **kw):
    return CronScheduleAction(task=task + " with enough words", schedule=spec, **kw)


@pytest.mark.parametrize("spec,minutes,want", [
    ("every day 09:00", 15, "every day 09:15"),
    ("every monday 23:50", 5, "every monday 23:55"),
    ("every monday 23:55", 10, None),                  # the move would change the day
    ("0 9 * * *", 15, "15 9 * * *"),
    ("0 9,13,17 * * *", 10, "10 9,13,17 * * *"),
    ("50 8 * * *", 15, "5 9 * * *"),
    ("50 8,23 * * *", 15, None),
    ("50 * * * *", 15, "5 * * * *"),
    ("*/30 * * * *", 5, "5-59/30 * * * *"),
    ("*/30 * * * *", 30, None),
    ("0 18 */3 * *", 10, "10 18 */3 * *"),
    ("2h", 5, None),                                   # counts from each run
    ("2026-11-20T09:00:00", 5, None),                  # a one-shot is a promise
])
def test_shift_spec(spec, minutes, want):
    got = shift_spec(spec, minutes)
    assert got == want
    if got:
        parse_schedule(got)


def test_shift_keeps_an_anchored_cycle_and_its_zone():
    got = shift_spec("every 3d from 2026-10-26T18:00Z", 10)
    assert got == "every 3d from 2026-10-26T18:10:00+00:00"
    assert parse_schedule(got).kind == "interval"


def _job(job_id, spec, *, payload=None, status="scheduled"):
    nxt = parse_schedule(spec).next_run_after(NOW)
    return types.SimpleNamespace(id=job_id, task=f"task {job_id}", schedule_spec=spec,
                                 next_run_at=nxt, status=status, payload=payload or {},
                                 created_at=NOW)


def test_free_time_stays_and_an_overlap_moves_past_the_whole_slot():
    jobs = [_job("a", "0 9 * * *")]
    assert place("30 9 * * *", {}, jobs, NOW).moved is False
    p = place("0 9 * * *", {}, jobs, NOW)
    assert p.moved and p.spec == "10 9 * * *" and p.overlap.job.id == "a"
    # 5 minutes after a job is still inside its slot; 10 minutes is outside it.
    assert place("5 9 * * *", {}, jobs, NOW).spec == "10 9 * * *"
    assert place("10 9 * * *", {}, jobs, NOW).moved is False


def test_a_daily_job_overlaps_a_weekly_job_on_its_day_only():
    jobs = [_job("weekly", "every friday 09:00")]
    p = place("every day 09:00", {}, jobs, NOW)
    assert p.moved and p.spec == "every day 09:10"


def test_several_jobs_spread_in_turn():
    jobs, specs = [], []
    for n in range(4):
        p = place("0 18 * * *", {}, jobs, NOW)
        specs.append(p.spec)
        jobs.append(_job(f"j{n}", p.spec))
    assert specs == ["0 18 * * *", "10 18 * * *", "20 18 * * *", "30 18 * * *"]


@pytest.mark.parametrize("payload", [{"read_verb": {"verb": "x"}}, {"write_verb": {"verb": "x"}},
                                     {"digest": True}, {"wake_agent": False}])
def test_a_run_with_no_model_holds_no_slot_and_does_not_move(payload):
    assert holds_a_slot(payload) is False
    assert place("0 9 * * *", payload, [_job("a", "0 9 * * *")], NOW).moved is False
    jobs = [_job("zero", "0 9 * * *", payload=payload)]
    assert place("0 9 * * *", {}, jobs, NOW).moved is False


def test_a_job_that_fills_the_clock_is_no_obstacle():
    jobs = [_job("watch", "*/5 * * * *")]
    assert place("0 9 * * *", {}, jobs, NOW).moved is False
    assert place("*/5 * * * *", {}, [_job("a", "0 9 * * *")], NOW).moved is False


def test_a_paused_or_stopped_job_holds_no_slot():
    for status in ("paused", "cancelled", "done"):
        assert place("0 9 * * *", {}, [_job("a", "0 9 * * *", status=status)], NOW).moved is False


def test_no_free_minute_keeps_the_time_and_names_the_overlap():
    jobs = [_job(f"j{m}", f"{m} 9 * * *") for m in range(0, 60, 10)]
    p = place("0 9 * * *", {}, jobs, NOW)
    assert p.moved is False and p.spec == "0 9 * * *" and p.overlap is not None


def test_a_bare_duration_and_a_one_shot_keep_their_time():
    jobs = [_job("a", "0 9 * * *")]
    assert place("2026-11-09T09:00:00", {}, jobs, NOW).moved is False
    assert place("2026-11-09T09:00:00", {}, jobs, NOW).overlap is not None


def test_a_bad_spec_comes_back_for_the_service_to_refuse():
    assert place("whenever", {}, [], NOW).spec == "whenever"


@pytest.mark.asyncio
async def test_schedule_moves_the_job_and_tells_the_agent(tmp_path):
    t = _tool(tmp_path)
    await t.cronjob_schedule(_sched("first", "0 18 * * *"), execution_context=_ctx())
    res = await t.cronjob_schedule(_sched("second", "0 18 * * *"), execution_context=_ctx())
    assert res.error is None
    assert "Moved from '0 18 * * *' to '10 18 * * *'" in res.extracted_content
    assert "first" in res.extracted_content and "overlaps" not in res.extracted_content
    specs = sorted(j.schedule_spec for j in t._cron_service.list_jobs(user_id="u1"))
    assert specs == ["0 18 * * *", "10 18 * * *"]


@pytest.mark.asyncio
async def test_exact_time_keeps_the_time_and_stays_named(tmp_path):
    t = _tool(tmp_path)
    await t.cronjob_schedule(_sched("first", "0 18 * * *"), execution_context=_ctx())
    res = await t.cronjob_schedule(_sched("market open", "0 18 * * *", exact_time=True),
                                   execution_context=_ctx())
    assert "Moved" not in res.extracted_content
    assert "overlaps" in res.extracted_content and "keeps its exact time" in res.extracted_content
    jobs = t._cron_service.list_jobs(user_id="u1")
    assert [j.schedule_spec for j in jobs] == ["0 18 * * *", "0 18 * * *"]


@pytest.mark.asyncio
async def test_edit_places_a_new_schedule_too(tmp_path):
    t = _tool(tmp_path)
    await t.cronjob_schedule(_sched("first", "0 18 * * *"), execution_context=_ctx())
    other = t._cron_service.schedule(task="second job", schedule_spec="0 7 * * *", user_id="u1")
    res = await t.cronjob_edit(CronEditAction(job_id=other.id, schedule="0 18 * * *"),
                               execution_context=_ctx())
    assert res.error is None and "Moved from" in res.extracted_content
    assert t._cron_service.store.get(other.id, user_id="u1").schedule_spec == "10 18 * * *"


@pytest.mark.asyncio
async def test_edit_does_not_move_a_job_off_its_own_slot(tmp_path):
    t = _tool(tmp_path)
    job = t._cron_service.schedule(task="only job", schedule_spec="0 18 * * *", user_id="u1")
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, schedule="5 18 * * *"),
                               execution_context=_ctx())
    assert "Moved" not in res.extracted_content
    assert t._cron_service.store.get(job.id, user_id="u1").schedule_spec == "5 18 * * *"


@pytest.mark.asyncio
async def test_list_names_the_free_slot_on_the_newer_job_and_the_edit_clears_it(tmp_path):
    t = _tool(tmp_path)
    svc = t._cron_service
    old = svc.schedule(task="older job", schedule_spec="0 18 * * *", user_id="u1")
    new = svc.schedule(task="newer job", schedule_spec="0 18 * * *", user_id="u1")
    res = await t.cronjob_list(CronListAction(), execution_context=_ctx())
    lines = res.extracted_content.splitlines()
    new_line = next(ln for ln in lines if new.id in ln and "newer job" in ln)
    assert f"overlaps `{old.id}`" in new_line and "free: '10 18 * * *'" in new_line
    assert "overlaps" not in next(ln for ln in lines if "older job" in ln)
    await t.cronjob_edit(CronEditAction(job_id=new.id, schedule="10 18 * * *"),
                         execution_context=_ctx())
    res = await t.cronjob_list(CronListAction(), execution_context=_ctx())
    assert "overlaps" not in res.extracted_content


@pytest.mark.asyncio
async def test_list_leaves_an_exact_job_alone_and_marks_the_other(tmp_path):
    t = _tool(tmp_path)
    svc = t._cron_service
    svc.schedule(task="older job", schedule_spec="0 18 * * *", user_id="u1")
    svc.schedule(task="owner typed", schedule_spec="0 18 * * *", user_id="u1", owner_seat=True)
    res = await t.cronjob_list(CronListAction(), execution_context=_ctx())
    lines = res.extracted_content.splitlines()
    assert "overlaps" in next(ln for ln in lines if "older job" in ln)
    assert "overlaps" not in next(ln for ln in lines if "owner typed" in ln)


def test_an_owner_seat_keeps_the_typed_time_and_gets_the_note(tmp_path):
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    svc.schedule(task="first job", schedule_spec="0 18 * * *", user_id="u1")
    job = svc.schedule(task="owner job", schedule_spec="0 18 * * *", user_id="u1", owner_seat=True)
    assert job.schedule_spec == "0 18 * * *" and job.payload["exact_time"] is True
    assert any("overlaps" in n for n in job.schedule_notes)


def test_the_table_ignores_an_unreadable_row():
    bad = _job("a", "0 9 * * *")
    bad.schedule_spec = "whenever"
    assert SlotTable([bad], NOW).overlap([NOW + timedelta(hours=2)]) is None


def test_operations_prints_a_job_time_in_real_utc(monkeypatch):
    """The block said UTC and printed the server clock."""
    import time as _time
    from core.status_render import _utc
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    _time.tzset()
    try:
        assert _utc("2026-11-09T09:00:00") == "2026-11-09 00:00"
        assert _utc(0) == "1970-01-01 00:00"
        assert _utc("not a time") == "not a time"
    finally:
        monkeypatch.delenv("TZ")
        _time.tzset()


def test_the_result_names_a_server_clock_that_is_not_utc(monkeypatch):
    import time as _time
    from tools.cronjob_tools import _server_clock_note
    monkeypatch.setenv("TZ", "UTC")
    _time.tzset()
    assert _server_clock_note() == ""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    _time.tzset()
    try:
        assert _server_clock_note() == " (server clock UTC+9:00)"
    finally:
        monkeypatch.delenv("TZ")
        _time.tzset()
