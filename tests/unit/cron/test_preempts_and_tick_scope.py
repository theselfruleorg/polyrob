"""057 WS-C — pre-emption is a job property, and a yield is a tick's own decision.

- ``payload.preempts`` decides who may INTERRUPT running work; ``payload.priority``
  only orders jobs within a tick. Prod's SAFETY and WATCHER rails are read-only
  and money-class, so they caused 5 of the 17 yields on 2026-09-19 for nothing.
- The yield probe runs INSIDE the TickLock (it ran before it, so under
  workers>1 two processes could both pre-empt the same goal for the same job).
- After a yield the tick runs ONLY the jobs that earned it; the rest wait.
"""
from datetime import datetime

import pytest

from cron.jobs import CronJob, CronJobStore, is_money_job, job_preempts
from cron.scheduler import CronScheduler

NOW = datetime(2026, 9, 20, 6, 0, 0)


def _job(jid="exit0001", task="EXIT RAIL", payload=None, due=NOW):
    return CronJob(id=jid, task=task, schedule_spec="0 * * * *", user_id="rob",
                   next_run_at=due, max_duration_seconds=60,
                   payload=payload if payload is not None else {"priority": "money"})


def test_preempts_defaults_to_the_money_class():
    assert job_preempts(_job()) is True
    assert job_preempts(_job(payload={})) is False
    assert job_preempts(_job(payload={"priority": "ops"})) is False


def test_an_explicit_preempts_flag_wins_over_the_class():
    """A read-only money rail can decline to interrupt work."""
    watcher = _job(payload={"priority": "money", "preempts": False})
    assert is_money_job(watcher) is True, "it is still ordered first within a tick"
    assert job_preempts(watcher) is False, "but it never interrupts a running goal"
    opted_in = _job(payload={"priority": "ops", "preempts": True})
    assert job_preempts(opted_in) is True


def test_set_preempts_merges_the_payload(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job(payload={"priority": "money", "deliver": "telegram"}))
    assert store.set_preempts("exit0001", False, user_id="rob")
    job = store.get("exit0001")
    assert job.payload["preempts"] is False
    assert job.payload["deliver"] == "telegram", "the rest of the payload survives"
    assert job_preempts(job) is False
    assert not store.set_preempts("exit0001", True, user_id="someone-else")


@pytest.mark.asyncio
async def test_after_a_yield_only_preempting_jobs_run_this_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_YIELD_FOR_MONEY_RAIL", "true")
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / "locks"))
    from core import interactive_gate as g
    g._busy_depth = 0
    g.mark_busy()  # a goal run holds the process
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job())                                   # pre-empting
    store.add(_job(jid="ops00001", task="LOG ROTATE", payload={}))  # not
    ran = []

    async def runner(job):
        ran.append(job.id)
        return True

    async def yield_hook(job):
        g.mark_idle()
        return ["goal-a"]

    sched = CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock"))
    sched.set_yield_hook(yield_hook)
    res = await sched.tick(now=NOW)

    assert ran == ["exit0001"], "the ops job must wait for the next tick"
    assert res.yielded == ["goal-a"]
    g._busy_depth = 0


@pytest.mark.asyncio
async def test_a_normal_tick_still_runs_everything_due(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / "locks"))
    from core import interactive_gate as g
    g._busy_depth = 0
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job())
    store.add(_job(jid="ops00001", task="LOG ROTATE", payload={}))
    ran = []

    async def runner(job):
        ran.append(job.id)
        return True

    sched = CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock"))
    res = await sched.tick(now=NOW)
    assert sorted(ran) == ["exit0001", "ops00001"]
    assert res.yielded == []


@pytest.mark.asyncio
async def test_the_yield_probe_runs_under_the_tick_lock(tmp_path, monkeypatch):
    """A second worker holding the lock must not get a second pre-emption."""
    monkeypatch.setenv("GOAL_YIELD_FOR_MONEY_RAIL", "true")
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / "locks"))
    from core import interactive_gate as g
    from cron.scheduler import TickLock
    g._busy_depth = 0
    g.mark_busy()
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job())
    yielded = []

    async def runner(job):
        return True

    async def yield_hook(job):
        yielded.append(job.id)
        return ["goal-a"]

    other = TickLock(str(tmp_path / "t.lock"))
    assert other.acquire(), "simulate another worker mid-tick"
    sched = CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock"))
    sched.set_yield_hook(yield_hook)
    res = await sched.tick(now=NOW)

    assert res.skipped_locked and yielded == [], \
        "the lock holder owns the pre-emption decision for this tick"
    other.release()
    g._busy_depth = 0


def test_a_yielded_tick_counts_as_activity():
    """The idle-backoff ticker must not read a pre-emption as an empty tick."""
    from cron.runner import _cron_tick_is_active
    from cron.scheduler import TickResult
    assert _cron_tick_is_active(TickResult()) is False
    assert _cron_tick_is_active(TickResult(yielded=["goal-a"])) is True


def test_a_money_job_is_never_change_gated(tmp_path, monkeypatch):
    """A4: the fingerprint cannot see a price move, so a $0 skip would be a lie."""
    monkeypatch.setenv("WAKE_CHANGE_GATE", "true")
    from cron import wake_gate
    gated_ops = _job(jid="ops00001", payload={"change_gated": True})
    gated_money = _job(payload={"priority": "money", "change_gated": True})
    assert wake_gate.gate_applies(gated_ops) is True
    assert wake_gate.gate_applies(gated_money) is False


def test_the_default_cap_reads_an_env_row(monkeypatch):
    from cron.jobs import default_max_duration_sec
    assert default_max_duration_sec() == 600
    monkeypatch.setenv("CRON_DEFAULT_MAX_DURATION_SEC", "900")
    assert default_max_duration_sec() == 900
    assert CronJob(id="x", task="t", schedule_spec="1h", user_id="rob",
                   next_run_at=None).max_duration_seconds == 900


def test_prune_only_removes_old_cancelled_rows(tmp_path):
    from datetime import timedelta
    store = CronJobStore(str(tmp_path / "cron.db"))
    old = _job(jid="old00001")
    old.created_at = NOW - timedelta(days=30)
    store.add(old)
    store.cancel("old00001")
    recent = _job(jid="new00001")
    recent.created_at = NOW - timedelta(days=1)
    store.add(recent)
    store.cancel("new00001")
    live = _job(jid="live0001")
    live.created_at = NOW - timedelta(days=30)
    store.add(live)

    removed = store.prune_cancelled(older_than_days=7, now=NOW)

    assert removed == 1
    ids = {j.id for j in store.list()}
    assert ids == {"new00001", "live0001"}, \
        "a live job is never pruned, however old"


def test_prune_cancelled_dry_run_counts_the_same_predicate(tmp_path):
    """C54 (2026-09-21): the preview is the store's own WHERE clause — a dry run
    counts exactly the rows a real prune deletes, and deletes nothing."""
    from datetime import timedelta
    store = CronJobStore(str(tmp_path / "cron.db"))
    old = _job(jid="old00001"); old.created_at = NOW - timedelta(days=30)
    store.add(old); store.cancel("old00001")
    new = _job(jid="new00001"); new.created_at = NOW - timedelta(days=1)
    store.add(new); store.cancel("new00001")
    live = _job(jid="live0001"); live.created_at = NOW - timedelta(days=30)
    store.add(live)

    assert store.prune_cancelled(older_than_days=7, now=NOW, dry_run=True) == 1
    assert {j.id for j in store.list()} == {"old00001", "new00001", "live0001"}
    assert store.prune_cancelled(older_than_days=7, now=NOW) == 1
    assert {j.id for j in store.list()} == {"new00001", "live0001"}
