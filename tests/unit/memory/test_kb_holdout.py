"""D4: ``payload.kb_holdout_pct`` skips KB auto-injection on a stable hash, and emits one event."""
import asyncio
import types
from datetime import datetime
from types import SimpleNamespace

import pytest

from agents.task.agent.core import memory_prefetch as mp
from core import autonomy_marker as am
from core.autonomy_marker import kb_holdout_applies
from cron.jobs import CronJobStore
from cron.service import CronService
from modules.memory import registry as R
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
from tools.cronjob_tools import CronEditAction, CronJobTool, CronScheduleAction

SLOT = datetime(2026, 1, 5, 9, 0)
JOB = "job-holdout"
SESSION = "sess-holdout"


def test_the_hash_is_stable_for_one_run_and_moves_with_the_schedule():
    answers = [kb_holdout_applies(JOB, SLOT, 50) for _ in range(5)]
    assert len(set(answers)) == 1
    slots = [datetime(2026, 1, d, h) for d in range(1, 29) for h in range(24)]
    held = sum(kb_holdout_applies(JOB, s, 20) for s in slots)
    assert 0.1 < held / len(slots) < 0.3


@pytest.mark.parametrize("pct,expected", [(100, True), (0, False), (None, False), ("50", False),
                                          (True, False), (101, False), (-1, False)])
def test_a_percent_outside_one_to_a_hundred_is_no_holdout(pct, expected):
    assert kb_holdout_applies(JOB, SLOT, pct) is expected


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setattr("core.config_policy.AutonomyConfig.kb_auto_prefetch",
                        staticmethod(lambda: True))
    prov = SqliteMemoryProvider(str(tmp_path / "m.db"))
    assert asyncio.run(prov.kb_replace_source(
        user_id="u", collection="docs", source_path="a.md", source_hash="h",
        chunks=["quarterly widget budget"]))
    monkeypatch.setattr(R, "get_memory_registry", lambda: SimpleNamespace(active=lambda: prov))
    events = []
    monkeypatch.setattr("agents.task.telemetry.memory_events.emit_memory_event",
                        lambda kind, **kw: events.append((kind, kw)))
    am.mark_autonomous(SESSION, cron_job_id=JOB)
    yield events
    am._SESSIONS.pop(SESSION, None)
    am._CRON_JOBS.pop(SESSION, None)
    am._JOB_KB_HOLDOUT.pop(JOB, None)


def _recall(session=SESSION):
    return asyncio.run(mp.build_prefetch_message("widget budget", session_id=session, user_id="u"))


def _holdouts(events):
    return [kw for kind, kw in events if kind == "memory_kb_holdout"]


def test_a_held_out_run_gets_no_kb_and_records_one_event(world):
    am.note_job(JOB, "task", kb_holdout_pct=100, scheduled_at=SLOT)
    assert _recall() is None
    assert _recall() is None  # the cadence fires again in the same run
    rows = _holdouts(world)
    assert len(rows) == 1
    assert rows[0]["scope"] == "kb_holdout" and rows[0]["job_id"] == JOB


def test_the_next_run_of_the_job_has_its_own_event(world):
    am.note_job(JOB, "task", kb_holdout_pct=100, scheduled_at=SLOT)
    _recall()
    am.note_job(JOB, "task", kb_holdout_pct=100, scheduled_at=datetime(2026, 1, 5, 10, 0))
    _recall()
    assert len(_holdouts(world)) == 2


def test_an_absent_key_leaves_the_kb_on_and_records_nothing(world):
    am.note_job(JOB, "task")
    msg = _recall()
    assert msg is not None and "widget" in str(msg.content)
    assert not _holdouts(world)


def test_an_owner_chat_is_never_held_out(world):
    am.note_job(JOB, "task", kb_holdout_pct=100, scheduled_at=SLOT)
    msg = _recall(session="owner-chat")
    assert msg is not None and "widget" in str(msg.content)
    assert not _holdouts(world)


def _turn():
    return types.SimpleNamespace(user_id="alice", role="orchestrator", is_sub_agent=False,
                                 metadata={"owner_text": "set it up"}, session_id=None,
                                 parent_session_id=None)


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "alice")
    t = object.__new__(CronJobTool)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def _schedule(tool, turn, **kw):
    params = CronScheduleAction(task="check the widget", schedule="every 2h", **kw)
    return asyncio.run(tool.cronjob_schedule(params, execution_context=turn))


@pytest.mark.parametrize("bad", [101, -1, "20", True, 1.5])
def test_the_tool_refuses_a_bad_percent(bad):
    with pytest.raises(ValueError):
        CronScheduleAction(task="t", schedule="every 2h", kb_holdout_pct=bad)
    with pytest.raises(ValueError):
        CronEditAction(job_id="j", kb_holdout_pct=bad)


def test_the_owner_sets_and_clears_the_percent_and_absent_stores_nothing(tool):
    assert not _schedule(tool, _turn()).error
    assert "kb_holdout_pct" not in tool._cron_service.list_jobs(user_id="alice")[0].payload
    assert not _schedule(tool, _turn(), kb_holdout_pct=0).error
    jobs = tool._cron_service.list_jobs(user_id="alice")
    assert all("kb_holdout_pct" not in j.payload for j in jobs)
    job = jobs[0]
    res = asyncio.run(tool.cronjob_edit(CronEditAction(job_id=job.id, kb_holdout_pct=20),
                                        execution_context=_turn()))
    assert not res.error
    stored = [j for j in tool._cron_service.list_jobs(user_id="alice") if j.id == job.id][0]
    assert stored.payload["kb_holdout_pct"] == 20
    assert not asyncio.run(tool.cronjob_edit(CronEditAction(job_id=job.id, kb_holdout_pct=0),
                                             execution_context=_turn())).error
    stored = [j for j in tool._cron_service.list_jobs(user_id="alice") if j.id == job.id][0]
    assert "kb_holdout_pct" not in stored.payload


def test_a_turn_that_is_not_the_owner_may_not_set_it(tool, monkeypatch):
    monkeypatch.setattr("tools.goal_tools.owner_seat_turn", lambda ctx: False)
    assert "kb_holdout_pct refused" in (_schedule(tool, _turn(), kb_holdout_pct=20).error or "")
