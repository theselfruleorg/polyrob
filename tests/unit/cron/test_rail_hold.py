"""116 W2.3 — an open owner question holds its own cron job.

Prod 2026-11-11: one job ran 24 times in 24 h with nothing to do while its own
question to the owner was open for 1.8 days. An ask with ``payload.holds_rail``
(set by ``owner_ask(holds_this_job=true)`` or, owner decision D2, after three idle
runs in a row) now holds the job: a due slot is a ``deferred`` row, $0, no session.
"""
import time
from datetime import datetime

import pytest

from agents.task.goals.board import GoalBoard
from core.cron_expect import holding_ask, job_held_by
from cron.jobs import CronJob, CronJobStore
from cron.scheduler import CronScheduler
from cron import rail_hold

NOW = datetime(2026, 11, 11, 18, 0, 0)


@pytest.fixture
def log(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    test_log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: test_log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    return test_log


def _job(**kw):
    base = dict(id="hold0001", task="collab outreach", schedule_spec="0 * * * *",
                user_id="alice", next_run_at=datetime(2026, 11, 11, 17, 0, 0),
                max_duration_seconds=60)
    base.update(kw)
    return CronJob(**base)


def _ask(board, job_id="hold0001", **extra):
    payload = {"origin": "agent", "rail_id": f"cron:{job_id}", "rail_kind": "cron", **extra}
    return board.create_ask(user_id="alice", what="expand the list or stop?",
                            extra_payload=payload, force=True)


def _home(tmp_path):
    from core.runtime_paths import cron_db_path, goals_db_path
    return CronJobStore(cron_db_path(str(tmp_path))), GoalBoard(goals_db_path(str(tmp_path)))


# --- the read ------------------------------------------------------------------

def test_holding_ask_reads_only_a_holding_open_ask_of_this_job():
    rows = [{"id": "a1", "title": "q1", "payload": {"rail_id": "cron:j1"}},
            {"id": "a2", "title": "q2", "payload": '{"rail_id": "cron:j2", "holds_rail": true}'},
            {"id": "a3", "title": "q3",
             "payload": {"rail_id": "cron:j1", "holds_rail": True, "holds_rail_via": "auto"}}]
    assert holding_ask(rows, "j1") == {"ask_id": "a3", "question": "q3", "via": "auto",
                                       "expires_at": None}
    assert holding_ask(rows, "j2")["via"] == "owner_ask"
    assert holding_ask(rows, "j3") is None


def test_an_expired_ask_holds_nothing():
    rows = [{"id": "a1", "title": "q", "payload": {"rail_id": "cron:j1", "holds_rail": True,
                                                   "expires_at": time.time() - 5}}]
    assert holding_ask(rows, "j1") is None


def test_job_held_by_reads_goals_db(tmp_path):
    store, board = _home(tmp_path)
    assert job_held_by(board.db_path, "alice", "hold0001") is None
    a = _ask(board, holds_rail=True)
    held = job_held_by(board.db_path, "alice", "hold0001")
    assert held["ask_id"] == a.id and held["via"] == "owner_ask"
    assert job_held_by(board.db_path, "bob", "hold0001") is None
    assert job_held_by(str(tmp_path / "absent.db"), "alice", "hold0001") is None


def test_an_unreadable_board_raises_and_the_scheduler_runs_the_job(tmp_path):
    bad = tmp_path / "goals.db"
    bad.write_text("not a database")
    with pytest.raises(Exception):
        job_held_by(str(bad), "alice", "hold0001")
    from core.runtime_paths import cron_db_path
    assert rail_hold.held_by(_job(), cron_db_path=cron_db_path(str(tmp_path))) is None


# --- the scheduler ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_held_job_is_deferred_with_no_run(tmp_path, log):
    store, board = _home(tmp_path)
    store.add(_job())
    a = _ask(board, holds_rail=True, holds_rail_via="owner_ask")
    ran = []

    async def runner(job):
        ran.append(job.id)
        return True

    res = await CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock")).tick(now=NOW)
    assert ran == [] and res.deferred == ["hold0001"]
    rows = [r for r in log.query(kind="cron_run") if r["attrs"].get("job_id") == "hold0001"]
    assert [r["attrs"]["outcome"] for r in rows] == ["deferred"]
    attrs = rows[0]["attrs"]
    assert attrs["reason"] == f"owner_ask:{a.id}" and attrs["hold"] == "owner_ask"
    assert attrs["held_by"] == a.id and rows[0]["session_id"] == ""
    job = store.get("hold0001")
    assert job.status == "scheduled" and job.next_run_at > NOW
    last = (job.payload or {}).get("last_outcome") or {}
    assert last.get("outcome") == "deferred" and int(last.get("streak") or 0) == 0


@pytest.mark.asyncio
async def test_an_answered_ask_releases_the_job(tmp_path, log):
    store, board = _home(tmp_path)
    store.add(_job())
    a = _ask(board, holds_rail=True)
    board.decide_ask(a.id, user_id="alice", approved=True, answer="stop")
    ran = []

    async def runner(job):
        ran.append(job.id)
        return True

    # the approval re-arms the job to the wall clock's "now" (rail_answers.rearm_cron_rail)
    from datetime import timedelta
    await CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock")).tick(
        now=datetime.now() + timedelta(minutes=1))
    assert ran == ["hold0001"]


@pytest.mark.asyncio
async def test_an_open_ask_without_the_hold_does_not_hold(tmp_path, log):
    store, board = _home(tmp_path)
    store.add(_job())
    _ask(board)
    ran = []

    async def runner(job):
        ran.append(job.id)
        return True

    await CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock")).tick(now=NOW)
    assert ran == ["hold0001"]


@pytest.mark.asyncio
async def test_a_held_one_shot_looks_again_later_not_every_tick(tmp_path, log):
    store, board = _home(tmp_path)
    store.add(_job(one_shot=True, schedule_spec="once"))
    _ask(board, holds_rail=True)

    async def runner(job):
        return True

    await CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock")).tick(now=NOW)
    job = store.get("hold0001")
    assert job.status == "scheduled"
    assert (job.next_run_at - NOW).total_seconds() >= 600


# --- the automatic hold (D2) -------------------------------------------------------

def test_three_idle_runs_with_the_ask_open_hold_the_job(tmp_path):
    store, board = _home(tmp_path)
    a = _ask(board)
    job = _job()
    assert rail_hold.observe_run(job, "idle", data_dir=str(tmp_path)) is None
    assert rail_hold.observe_run(job, "unverified", data_dir=str(tmp_path)) is None
    held = rail_hold.observe_run(job, "idle", data_dir=str(tmp_path))
    assert held is not None and held.id == a.id
    p = board.get(a.id).payload
    assert p["holds_rail"] is True and p["holds_rail_via"] == "auto"
    assert job_held_by(board.db_path, "alice", "hold0001")["via"] == "auto"
    # held already: a fourth idle run does not hold it "newly" again
    assert rail_hold.observe_run(job, "idle", data_dir=str(tmp_path)) is None


def test_a_run_with_effect_resets_the_count(tmp_path):
    store, board = _home(tmp_path)
    a = _ask(board)
    job = _job()
    rail_hold.observe_run(job, "idle", data_dir=str(tmp_path))
    rail_hold.observe_run(job, "idle", data_dir=str(tmp_path))
    rail_hold.observe_run(job, "effect", data_dir=str(tmp_path))
    assert rail_hold.IDLE_RUNS_KEY not in board.get(a.id).payload
    assert rail_hold.observe_run(job, "idle", data_dir=str(tmp_path)) is None
    assert job_held_by(board.db_path, "alice", "hold0001") is None


def test_no_open_ask_counts_nothing(tmp_path):
    _home(tmp_path)
    assert rail_hold.observe_run(_job(), "idle", data_dir=str(tmp_path)) is None


def test_the_threshold_is_the_owner_decision():
    assert rail_hold.AUTO_HOLD_AFTER == 3


@pytest.mark.asyncio
async def test_the_runner_counts_idle_done_runs(tmp_path, monkeypatch, log):
    from agents.task.runtime.run_outcome import RunOutcome
    from cron.runner import make_agent_runner
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "absent" / "bot.db"))
    told = []

    async def _deliver(container, user_id, text, **kw):
        told.append((text, kw.get("source")))
        return "sent"
    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)

    class _Agent:
        container = None

        async def create_session(self, *, user_id, request):
            return {"id": "sess-idle"}

    async def fake_run(*a, **k):
        return RunOutcome(session_id="sess-idle", status="ok", done_called=True,
                          done_text="nothing new", user_messages=[])
    monkeypatch.setattr("cron.runner._run_task_to_outcome", fake_run)
    store, board = _home(tmp_path)
    a = _ask(board)
    runner = make_agent_runner(_Agent(), data_dir=str(tmp_path))
    for _ in range(3):
        assert await runner(_job(one_shot=True, payload={})) is True
    assert job_held_by(board.db_path, "alice", "hold0001")["ask_id"] == a.id
    assert [src for _t, src in told if src == "cron_rail_hold"] == ["cron_rail_hold"]
