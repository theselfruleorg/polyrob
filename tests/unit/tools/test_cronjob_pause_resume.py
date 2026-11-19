"""120 W2: the agent pauses and resumes its own cron jobs, and reads a job's last runs.

The resume rule reads the pause note the row holds (``payload.paused``):
the agent's own pause -> the agent; the failure limit -> a genuine owner turn only;
an owner pause, a pause scope or a missing note -> nobody here (fail closed)."""
import asyncio
import json
import types
from datetime import datetime, timedelta

import pytest

from core import autonomy_marker
from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import (CronCancelAction, CronJobTool, CronPauseAction, CronResumeAction,
                                 CronRunNowAction, CronShowAction)

OWNER = "alice"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: OWNER)
    monkeypatch.setattr("core.cron_lanes.check_pinned_skills", lambda *a, **k: None)
    monkeypatch.setattr("cron.service.scheduler_off_note", lambda: "")
    yield
    for sid in ("s-auto", "s-auto-own"):
        autonomy_marker._SESSIONS.pop(sid, None)
        autonomy_marker._CRON_JOBS.pop(sid, None)


def _tool(tmp_path) -> CronJobTool:
    tool = object.__new__(CronJobTool)
    tool._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return tool


def _owner_turn():
    return types.SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                                 metadata={}, session_id="s-owner", parent_session_id=None)


def _auto_turn(job_id: str = "another-job", sid: str = "s-auto"):
    autonomy_marker.mark_autonomous(sid, cron_job_id=job_id)
    return types.SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                                 metadata={}, session_id=sid, parent_session_id=None)


def _leaf_turn():
    return types.SimpleNamespace(user_id=OWNER, role="leaf", is_sub_agent=True, metadata={},
                                 session_id="s-leaf", parent_session_id="s-owner")


def _job(tool, *, payload=None, spec="every 1d"):
    return tool._cron_service.schedule(task="post the daily summary", schedule_spec=spec,
                                       user_id=OWNER, payload=payload or {}, via="test")


def _set_paused(tool, job_id: str, note):
    """Pause the row with an exact note (or none at all: an old row)."""
    store = tool._cron_service.store
    assert store.pause(job_id, user_id=OWNER, note=note or {})
    if note is None:
        import sqlite3
        con = sqlite3.connect(store.db_path)
        con.execute("UPDATE cron_jobs SET payload=json_remove(payload, '$.paused') WHERE id=?",
                    (job_id,))
        con.commit()
        con.close()


def _run(coro):
    return asyncio.run(coro)


# --- the resume table (§3.2), one parametrized test ----------------------------------------

@pytest.mark.parametrize("note, turn, may", [
    ({"by": OWNER, "via": "agent"}, "owner", True),
    ({"by": OWNER, "via": "agent"}, "auto", True),
    ({"by": OWNER, "via": "owner_turn"}, "owner", True),
    ({"by": OWNER, "via": "owner_turn"}, "auto", False),
    ({"by": "failure_streak", "via": "scheduler"}, "owner", True),
    ({"by": "failure_streak", "via": "scheduler"}, "auto", False),
    ({"by": OWNER, "via": "telegram"}, "owner", False),
    ({"by": OWNER, "via": "telegram"}, "auto", False),
    ({"by": "pause_scope", "via": "pause"}, "owner", False),
    (None, "owner", False),
    ("not a mapping", "owner", False),
])
def test_the_resume_table(tmp_path, monkeypatch, note, turn, may):
    monkeypatch.setattr("tools.goal_tools.autonomous_run_may_schedule", lambda ctx: True)
    tool = _tool(tmp_path)
    job = _job(tool)
    if isinstance(note, str):
        _set_paused(tool, job.id, {"by": OWNER, "via": "agent"})
        import sqlite3
        con = sqlite3.connect(tool._cron_service.store.db_path)
        con.execute("UPDATE cron_jobs SET payload=json_set(payload, '$.paused', ?) WHERE id=?",
                    (note, job.id))
        con.commit()
        con.close()
    else:
        _set_paused(tool, job.id, note)
    ctx = _owner_turn() if turn == "owner" else _auto_turn()
    res = _run(tool.cronjob_resume(CronResumeAction(job_id=job.id), execution_context=ctx))
    after = tool._cron_service.store.get(job.id, user_id=OWNER)
    if may:
        assert not res.error, res.error
        assert after.status == "scheduled" and "Resumed job" in res.extracted_content
    else:
        assert res.error and "stays paused" in res.error
        assert after.status == "paused"


def test_a_failure_pause_on_an_autonomous_turn_names_the_owner_path(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.goal_tools.autonomous_run_may_schedule", lambda ctx: True)
    tool = _tool(tmp_path)
    job = _job(tool)
    _set_paused(tool, job.id, {"by": "failure_streak", "via": "scheduler"})
    res = _run(tool.cronjob_resume(CronResumeAction(job_id=job.id), execution_context=_auto_turn()))
    assert f"/job resume {job.id[:8]}" in res.error and "failure limit" in res.error


def test_an_owner_pause_is_refused_and_names_the_owner_path(tmp_path):
    tool = _tool(tmp_path)
    job = _job(tool)
    tool._cron_service.pause(job.id, user_id=OWNER, via="telegram")
    res = _run(tool.cronjob_resume(CronResumeAction(job_id=job.id),
                                   execution_context=_owner_turn()))
    assert res.error and f"/job resume {job.id[:8]}" in res.error


def test_own_pause_in_a_supervised_regime_follows_the_run_now_seat_rule(tmp_path, monkeypatch):
    """A resumed job runs past this turn: an autonomous run that may not schedule may
    not resume either (the cronjob_run_now rule)."""
    monkeypatch.setattr("tools.goal_tools.autonomous_run_may_schedule", lambda ctx: False)
    tool = _tool(tmp_path)
    job = _job(tool)
    tool._cron_service.pause(job.id, user_id=OWNER, via="agent")
    res = _run(tool.cronjob_resume(CronResumeAction(job_id=job.id), execution_context=_auto_turn()))
    assert res.error and "cronjob_resume denied" in res.error
    assert tool._cron_service.store.get(job.id, user_id=OWNER).status == "paused"


# --- pause ------------------------------------------------------------------------------

def test_pause_then_resume_on_the_agents_own_word(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.goal_tools.autonomous_run_may_schedule", lambda ctx: True)
    tool = _tool(tmp_path)
    job = _job(tool)
    res = _run(tool.cronjob_pause(CronPauseAction(job_id=job.id), execution_context=_auto_turn()))
    assert not res.error and "cronjob_resume resumes it" in res.extracted_content
    row = tool._cron_service.store.get(job.id, user_id=OWNER)
    assert row.status == "paused" and row.payload["paused"]["via"] == "agent"
    res = _run(tool.cronjob_resume(CronResumeAction(job_id=job.id), execution_context=_auto_turn()))
    assert not res.error


def test_an_owner_turn_pause_records_the_owner_turn(tmp_path):
    tool = _tool(tmp_path)
    job = _job(tool)
    res = _run(tool.cronjob_pause(CronPauseAction(job_id=job.id), execution_context=_owner_turn()))
    assert not res.error
    assert tool._cron_service.store.get(job.id, user_id=OWNER).payload["paused"]["via"] == \
        "owner_turn"


def test_the_agent_never_pauses_the_job_its_run_runs_in(tmp_path):
    tool = _tool(tmp_path)
    job = _job(tool)
    ctx = _auto_turn(job_id=job.id, sid="s-auto-own")
    res = _run(tool.cronjob_pause(CronPauseAction(job_id=job.id), execution_context=ctx))
    assert res.error and "this run runs in" in res.error
    assert tool._cron_service.store.get(job.id, user_id=OWNER).status == "scheduled"


@pytest.mark.parametrize("verb", ["pause", "resume"])
def test_a_mandate_job_moves_only_on_an_owner_turn(tmp_path, monkeypatch, verb):
    monkeypatch.setattr("tools.goal_tools.autonomous_run_may_schedule", lambda ctx: True)
    tool = _tool(tmp_path)
    job = _job(tool, payload={"mandate": "m1"})
    if verb == "resume":
        tool._cron_service.pause(job.id, user_id=OWNER, via="agent")
        call = tool.cronjob_resume(CronResumeAction(job_id=job.id), execution_context=_auto_turn())
    else:
        call = tool.cronjob_pause(CronPauseAction(job_id=job.id), execution_context=_auto_turn())
    res = _run(call)
    assert res.error and "trading mandate" in res.error and f"cronjob_{verb}" in res.error
    owner = (tool.cronjob_resume(CronResumeAction(job_id=job.id), execution_context=_owner_turn())
             if verb == "resume" else
             tool.cronjob_pause(CronPauseAction(job_id=job.id), execution_context=_owner_turn()))
    res = _run(owner)
    assert verb == "resume" or not res.error


@pytest.mark.parametrize("verb", ["pause", "resume"])
def test_a_leaf_is_refused(tmp_path, verb):
    tool = _tool(tmp_path)
    job = _job(tool)
    model = CronPauseAction if verb == "pause" else CronResumeAction
    res = _run(getattr(tool, f"cronjob_{verb}")(model(job_id=job.id),
                                                execution_context=_leaf_turn()))
    assert res.error and "sub-agent" in res.error


def test_a_pause_of_a_running_job_carries_the_cut_note(tmp_path):
    tool = _tool(tmp_path)
    job = _job(tool)
    import sqlite3
    con = sqlite3.connect(tool._cron_service.store.db_path)
    con.execute("UPDATE cron_jobs SET status='running', "
                "payload=json_set(payload, '$.cut_run', 'restart') WHERE id=?", (job.id,))
    con.commit()
    con.close()
    res = _run(tool.cronjob_pause(CronPauseAction(job_id=job.id), execution_context=_owner_turn()))
    assert not res.error
    assert "cut by a restart" in res.extracted_content


def test_resume_of_a_stopped_job_says_it_is_stopped(tmp_path):
    tool = _tool(tmp_path)
    job = _job(tool, payload={"rig": "research"})
    tool._cron_service.cancel(job.id, user_id=OWNER, via="telegram")
    res = _run(tool.cronjob_resume(CronResumeAction(job_id=job.id),
                                   execution_context=_owner_turn()))
    assert res.error and "stopped, not paused" in res.error
    assert "does not resume" in res.error and "lane `research`" in res.error


def test_resume_of_a_one_time_job_whose_time_passed_runs_next_tick(tmp_path):
    tool = _tool(tmp_path)
    when = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
    job = _job(tool, spec=when)
    assert job.one_shot
    tool._cron_service.pause(job.id, user_id=OWNER, via="agent")
    tool._cron_service._now = lambda: datetime.now() + timedelta(hours=1)
    res = _run(tool.cronjob_resume(CronResumeAction(job_id=job.id),
                                   execution_context=_owner_turn()))
    assert not res.error and "runs on the next tick" in res.extracted_content


# --- cronjob_run_now names the right path -------------------------------------------------

@pytest.mark.parametrize("via, needle", [
    ("agent", "cronjob_resume(job_id="),
    ("telegram", "the owner resumes it (/job resume"),
])
def test_run_now_on_a_paused_job_names_the_path(tmp_path, via, needle):
    tool = _tool(tmp_path)
    job = _job(tool)
    tool._cron_service.pause(job.id, user_id=OWNER, via=via)
    res = _run(tool.cronjob_run_now(CronRunNowAction(job_id=job.id),
                                    execution_context=_owner_turn()))
    assert res.error and needle in res.error
    assert "only the owner resumes it" not in res.error


# --- cronjob_show(runs=N) ----------------------------------------------------------------

def _log_with_runs(tmp_path, job_id: str):
    from core.event_log import TelemetryEventLog
    path = str(tmp_path / "telemetry_events.db")
    log = TelemetryEventLog(path)
    for i, outcome in enumerate(["done", "started", "failed", "done"]):
        log.record("cron_run", user_id=OWNER, session_id=f"s{i}", source="cron",
                   attrs={"job_id": job_id, "outcome": outcome, "reason": f"r{i}",
                          "cost_usd": 0.01})
    log.record("cron_run", user_id=OWNER, session_id="x", source="cron",
               attrs={"job_id": "another", "outcome": "done"})
    return path


def test_show_lists_the_last_runs_of_that_job_only(tmp_path, monkeypatch):
    tool = _tool(tmp_path)
    job = _job(tool)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", _log_with_runs(tmp_path, job.id))
    res = _run(tool.cronjob_show(CronShowAction(job_id=job.id, runs=2),
                                 execution_context=_owner_turn()))
    text = res.extracted_content
    assert "last 2 runs" in text
    run_lines = [ln for ln in text.splitlines() if ln.startswith("  - ")]
    assert len(run_lines) == 2
    assert "done (r3)" in run_lines[0] and "failed (r2)" in run_lines[1]
    assert "started" not in text.split("last 2 runs")[1].split("task:")[0]


def test_show_without_runs_reads_no_log(tmp_path):
    tool = _tool(tmp_path)
    job = _job(tool)
    res = _run(tool.cronjob_show(CronShowAction(job_id=job.id), execution_context=_owner_turn()))
    assert "runs (the run log)" not in res.extracted_content


def test_an_unreadable_run_log_is_named_never_no_runs(tmp_path, monkeypatch):
    tool = _tool(tmp_path)
    job = _job(tool)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "absent" / "t.db"))
    res = _run(tool.cronjob_show(CronShowAction(job_id=job.id, runs=5),
                                 execution_context=_owner_turn()))
    assert "could not be read" in res.extracted_content
    assert "none recorded" not in res.extracted_content


def test_runs_is_capped_at_twenty():
    with pytest.raises(Exception):
        CronShowAction(job_id="x", runs=21)


# --------------------------------------------------------------------------- the owner's pause wins

def test_an_owner_pause_takes_over_the_agents_pause(tmp_path, monkeypatch):
    """The owner pauses a job the agent already paused: the note becomes the
    owner's, so an autonomous run can no longer lift it."""
    monkeypatch.setattr("tools.goal_tools.autonomous_run_may_schedule", lambda ctx: True)
    tool = _tool(tmp_path)
    job = _job(tool)
    assert not _run(tool.cronjob_pause(CronPauseAction(job_id=job.id),
                                       execution_context=_auto_turn())).error
    svc = tool._cron_service
    assert svc.pause(job.id, user_id=OWNER, via="telegram")          # the owner's seat
    assert svc.store.get(job.id, user_id=OWNER).payload["paused"]["via"] == "telegram"
    res = _run(tool.cronjob_resume(CronResumeAction(job_id=job.id), execution_context=_auto_turn()))
    assert res.error and "/job resume" in res.error
    assert svc.store.get(job.id, user_id=OWNER).status == "paused"


def test_the_agent_never_takes_over_an_owner_pause(tmp_path):
    tool = _tool(tmp_path)
    job = _job(tool)
    svc = tool._cron_service
    assert svc.pause(job.id, user_id=OWNER, via="cli")
    assert not svc.pause(job.id, user_id=OWNER, via="agent")
    assert not svc.pause(job.id, user_id=OWNER, via="owner_turn")
    assert svc.store.get(job.id, user_id=OWNER).payload["paused"]["via"] == "cli"


def test_an_owner_turn_pause_takes_over_the_agents_own_only(tmp_path):
    tool = _tool(tmp_path)
    job = _job(tool)
    svc = tool._cron_service
    assert svc.pause(job.id, user_id=OWNER, via="agent")
    assert svc.pause(job.id, user_id=OWNER, via="owner_turn")
    assert svc.store.get(job.id, user_id=OWNER).payload["paused"]["via"] == "owner_turn"


def test_a_resume_misses_when_the_note_changed_since_it_was_judged(tmp_path):
    """The rule read `via=agent`; the owner took the pause over before the write."""
    tool = _tool(tmp_path)
    job = _job(tool)
    svc = tool._cron_service
    assert svc.pause(job.id, user_id=OWNER, via="agent")
    assert svc.pause(job.id, user_id=OWNER, via="telegram")
    assert not svc.resume(job.id, user_id=OWNER, via="agent", note_via="agent")
    assert svc.store.get(job.id, user_id=OWNER).status == "paused"
    assert svc.resume(job.id, user_id=OWNER, via="telegram")           # the owner's seat still lifts it


@pytest.mark.parametrize("note,turn,cancelled", [
    ({"by": "owner", "via": "telegram"}, "auto", False),
    ({"by": "agent", "via": "owner_turn"}, "auto", False),
    ({"by": "agent", "via": "agent"}, "auto", True),
    ({"by": "owner", "via": "telegram"}, "owner", True),
])
def test_a_cancel_never_lifts_a_pause_the_caller_may_not_resume(tmp_path, note, turn, cancelled):
    """A cancel and a new schedule of the same task would lift an owner pause."""
    tool = _tool(tmp_path)
    job = _job(tool)
    _set_paused(tool, job.id, note)
    ctx = _owner_turn() if turn == "owner" else _auto_turn()
    res = _run(tool.cronjob_cancel(CronCancelAction(job_id=job.id), ctx))
    row = tool._cron_service.store.get(job.id, user_id=OWNER)
    if cancelled:
        assert not res.error and (row is None or row.status != "paused")
    else:
        assert res.error and "may not cancel" in res.error and row.status == "paused"
