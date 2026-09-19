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
    # CAS: only the first claim of a 'scheduled' job wins; a second returns False.
    s = _store(tmp_path)
    s.add(_job(id="c1"))
    assert s.claim_for_run("c1") is True
    assert s.get("c1").status == "running"
    assert s.claim_for_run("c1") is False  # already running


def test_reclaim_not_called_in_init_does_not_reset_live_running(tmp_path):
    # A live 'running' job must survive a second CronJobStore being constructed
    # (e.g. the agent-facing CronJobTool) — reclaim no longer fires from __init__.
    db = str(tmp_path / "cron.db")
    s1 = CronJobStore(db)
    s1.add(_job(id="r1"))
    assert s1.claim_for_run("r1") is True  # now 'running'
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
