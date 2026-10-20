"""P5b — SQLite-backed cron job store."""
from datetime import datetime, timedelta

import pytest

from cron.jobs import CronJob, CronJobStore


def _store(tmp_path):
    return CronJobStore(str(tmp_path / "cron.db"))


def _job(**kw):
    base = dict(
        id="j1", task="do the thing", schedule_spec="*/15 * * * *", user_id="u1",
        next_run_at=datetime(2026, 6, 6, 12, 0, 0), one_shot=False,
        skip_memory=True, max_duration_seconds=180, payload={"provider": "gemini"},
    )
    base.update(kw)
    return CronJob(**base)


def test_add_and_get_round_trip(tmp_path):
    s = _store(tmp_path)
    s.add(_job())
    got = s.get("j1")
    assert got is not None
    assert got.task == "do the thing"
    assert got.payload == {"provider": "gemini"}
    assert got.skip_memory is True
    assert got.enabled is True
    assert got.status == "scheduled"


def test_list_filters_by_user(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="a", user_id="u1"))
    s.add(_job(id="b", user_id="u2"))
    assert {j.id for j in s.list(user_id="u1")} == {"a"}
    assert {j.id for j in s.list()} == {"a", "b"}


def test_due_returns_only_past_due_enabled(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="past", next_run_at=datetime(2026, 6, 6, 11, 0, 0)))
    s.add(_job(id="future", next_run_at=datetime(2026, 6, 6, 13, 0, 0)))
    now = datetime(2026, 6, 6, 12, 0, 0)
    assert {j.id for j in s.due(now)} == {"past"}


def test_update_after_run(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="a"))
    s.update_after_run(
        "a", last_run_at=datetime(2026, 6, 6, 12, 0, 0),
        next_run_at=datetime(2026, 6, 6, 12, 15, 0), status="scheduled",
    )
    got = s.get("a")
    assert got.last_run_at == datetime(2026, 6, 6, 12, 0, 0)
    assert got.next_run_at == datetime(2026, 6, 6, 12, 15, 0)


def test_cancel_disables_and_excludes_from_due(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="a", next_run_at=datetime(2026, 6, 6, 11, 0, 0)))
    assert s.cancel("a") is True
    got = s.get("a")
    assert got.enabled is False and got.status == "cancelled"
    assert s.due(datetime(2026, 6, 6, 12, 0, 0)) == []


def test_one_shot_completion_clears_next_run(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="a", one_shot=True, next_run_at=datetime(2026, 6, 6, 11, 0, 0)))
    s.update_after_run("a", last_run_at=datetime(2026, 6, 6, 12, 0, 0),
                       next_run_at=None, status="done")
    got = s.get("a")
    assert got.next_run_at is None and got.status == "done"
    assert s.due(datetime(2026, 6, 6, 13, 0, 0)) == []


def test_cancel_missing_returns_false(tmp_path):
    s = _store(tmp_path)
    assert s.cancel("nope") is False


def test_claim_for_run_is_cas(tmp_path):
    # CAS: only the first claim of a 'scheduled' job wins; a second returns None.
    s = _store(tmp_path)
    s.add(_job(id="c1"))
    assert s.claim_for_run("c1").id == "c1"
    assert s.get("c1").status == "running"
    assert s.claim_for_run("c1") is None  # already running


def test_reclaim_not_called_in_init_does_not_reset_live_running(tmp_path):
    # A live 'running' job must survive a second CronJobStore being constructed
    # (e.g. the agent-facing CronJobTool) — reclaim no longer fires from __init__.
    db = str(tmp_path / "cron.db")
    s1 = CronJobStore(db)
    s1.add(_job(id="r1"))
    assert s1.claim_for_run("r1") is not None  # now 'running'
    # Second store instance (mid-tick) must NOT reclaim the live job.
    CronJobStore(db)
    assert s1.get("r1").status == "running"


def test_reclaim_stale_running_resets_orphans(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="o1"))
    s.claim_for_run("o1")  # 'running'
    assert s.reclaim_stale_running() == 1
    assert s.get("o1").status == "scheduled"


# --- 056 WS5 (2026-09-19): owner/ops seat can re-time a job and class it ----------

def test_set_schedule_recomputes_next_run(tmp_path):
    from datetime import datetime
    from cron.jobs import CronJob, CronJobStore
    s = CronJobStore(str(tmp_path / "c.db"))
    s.add(CronJob(id="j1", task="SAFETY", schedule_spec="0 */2 * * *", user_id="rob",
                  next_run_at=datetime(2026, 9, 19, 6, 0)))
    ok = s.set_schedule("j1", "20 */2 * * *", now=datetime(2026, 9, 19, 5, 50), user_id="rob")
    assert ok
    j = s.get("j1")
    assert j.schedule_spec == "20 */2 * * *"
    assert j.next_run_at == datetime(2026, 9, 19, 6, 20)
    assert not s.set_schedule("j1", "not a schedule", now=datetime(2026, 9, 19, 5, 50))


def test_set_priority_merges_payload(tmp_path):
    from datetime import datetime
    from cron.jobs import CronJob, CronJobStore, is_money_job
    s = CronJobStore(str(tmp_path / "c.db"))
    s.add(CronJob(id="j1", task="EXIT", schedule_spec="0 * * * *", user_id="rob",
                  next_run_at=datetime(2026, 9, 19, 6, 0), payload={"deliver": "telegram"}))
    assert s.set_priority("j1", "money", user_id="rob")
    j = s.get("j1")
    assert j.payload == {"deliver": "telegram", "priority": "money"} and is_money_job(j)
    assert not s.set_priority("j1", "urgent")  # unknown class refused
    assert not s.set_priority("j1", "money", user_id="someone-else")


def test_done_write_disables_the_job(tmp_path):
    """081 WS-6.1: a job that reaches 'done' is disabled in the same write."""
    s = _store(tmp_path)
    s.add(_job(id="a", one_shot=True))
    claimed = s.claim_for_run("a")
    s.update_after_run("a", last_run_at=datetime(2026, 6, 6, 12, 0, 0),
                       next_run_at=None, status="done", expected=claimed)
    got = s.get("a")
    assert (got.status, got.enabled) == ("done", False)
    assert "a" not in {j.id for j in s.list(enabled_only=True)}
    # a recurring reschedule stays enabled
    s.add(_job(id="b"))
    claimed = s.claim_for_run("b")
    s.update_after_run("b", last_run_at=datetime(2026, 6, 6, 12, 0, 0),
                       next_run_at=datetime(2026, 6, 6, 12, 15, 0),
                       status="scheduled", expected=claimed)
    assert s.get("b").enabled is True
    # the un-CAS path too
    s.add(_job(id="c"))
    s.update_after_run("c", last_run_at=datetime(2026, 6, 6, 12, 0, 0),
                       next_run_at=None, status="done")
    assert s.get("c").enabled is False


def test_failed_write_disables_the_job_like_done(tmp_path):
    """A one-shot that ends 'failed' is terminal too: disabled in the same write,
    on the CAS path and the plain path, and an old failed+enabled row is
    disabled when the store opens."""
    import cron.jobs as jobs_mod
    s = _store(tmp_path)
    s.add(_job(id="f", one_shot=True))
    claimed = s.claim_for_run("f")
    s.update_after_run("f", last_run_at=datetime(2026, 6, 6, 12, 0, 0),
                       next_run_at=None, status="failed", expected=claimed)
    assert (s.get("f").status, s.get("f").enabled) == ("failed", False)
    s.add(_job(id="g"))
    s.update_after_run("g", last_run_at=datetime(2026, 6, 6, 12, 0, 0),
                       next_run_at=None, status="failed")
    assert s.get("g").enabled is False
    s.add(_job(id="old", status="failed", enabled=True))
    jobs_mod._CLEARED_STORES.discard(s.db_path)
    assert _store(tmp_path).get("old").enabled is False


def test_store_open_clears_done_enabled_rows_and_dead_payload_keys(tmp_path):
    """081 WS-6.1/6.3: an old row with status done + enabled=1 is disabled, and
    the deleted authorship keys are dropped, when the store opens."""
    import cron.jobs as jobs_mod
    s = _store(tmp_path)
    s.add(_job(id="old", status="done", enabled=True,
               payload={"provider": "gemini", "authored_by": "agent",
                        "adopted_skills_digest": "abc"}))
    s.add(_job(id="live"))
    jobs_mod._CLEARED_STORES.discard(s.db_path)
    s2 = _store(tmp_path)
    old = s2.get("old")
    assert old.enabled is False
    assert old.payload == {"provider": "gemini"}
    assert s2.get("live").enabled is True
