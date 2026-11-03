"""098 MO-09: cron_jobs status is one machine; a running row has an owner and a lease."""
import time
from datetime import datetime

import pytest

import cron.jobs as jobs_mod
from cron.job_machine import CRON_JOB_MACHINE
from cron.jobs import CronJob, CronJobStore
from core.sqlite_util import execute_retry


def _job(**kw):
    base = dict(id="j1", task="t", schedule_spec="*/15 * * * *", user_id="u1",
                next_run_at=datetime(2026, 6, 6, 11, 0), payload={})
    base.update(kw)
    return CronJob(**base)


@pytest.fixture
def store(tmp_path):
    return CronJobStore(str(tmp_path / "cron.db"))


def test_table_well_formed():
    assert CRON_JOB_MACHINE.validate() == []


def test_cancel_during_run_then_shutdown_requeue_stays_cancelled(store):
    store.add(_job())
    assert store.claim_for_run("j1") is not None
    assert store.cancel("j1", note={"by": "owner"})
    assert store.set_status("j1", "scheduled") is False     # shutdown requeue / pause cut
    row = store.get("j1")
    assert (row.status, row.enabled) == ("cancelled", False)
    assert store.cancel("j1") is True                        # idempotent
    assert store.cancel("nope") is False


def test_reclaim_only_expired_lease(store):
    store.add(_job())
    store.add(_job(id="j2"))
    store.claim_for_run("j1")
    store.claim_for_run("j2")
    execute_retry(store.db_path, "UPDATE cron_jobs SET lease_until=? WHERE id='j2'",
                  (time.time() - 5,))
    assert store.reclaim_stale_running(under_lock=False) == 1
    assert store.get("j1").status == "running"
    assert store.get("j2").status == "scheduled"


def test_stolen_lock_does_not_double_run(tmp_path):
    db = str(tmp_path / "cron.db")
    a, b = CronJobStore(db), CronJobStore(db)
    a.add(_job())
    assert a.claim_for_run("j1") is not None
    # worker B stole the tick lock: its reclaim must leave A's live run alone
    assert b.reclaim_stale_running() == 0
    assert b.due(datetime(2026, 6, 6, 12, 0)) == []
    assert b.claim_for_run("j1") is None


def test_legacy_running_row_needs_the_lock(store):
    store.add(_job())
    execute_retry(store.db_path, "UPDATE cron_jobs SET status='running' WHERE id='j1'")
    assert store.reclaim_stale_running(under_lock=False) == 0
    assert store.reclaim_stale_running() == 1


def test_scheduled_disabled_rows_normalised_to_cancelled(tmp_path):
    s = CronJobStore(str(tmp_path / "cron.db"))
    s.add(_job(enabled=False))
    s.add(_job(id="p", status="paused", enabled=False))
    s.add(_job(id="ok"))
    jobs_mod._CLEARED_STORES.discard(s.db_path)
    s2 = CronJobStore(s.db_path)
    assert s2.get("j1").status == "cancelled"
    assert s2.get("p").status == "cancelled"
    assert s2.get("ok").status == "scheduled"


def test_unguarded_after_run_cannot_revive_cancelled(store):
    store.add(_job())
    store.cancel("j1")
    store.update_after_run("j1", last_run_at=datetime(2026, 6, 6), next_run_at=None,
                           status="scheduled")
    assert store.get("j1").status == "cancelled"


def test_a_reclaimed_run_cannot_end_the_run_that_took_the_row(store):
    """098 review: run A's lease runs out, the row is reclaimed and claimed by
    run B; when A finally ends, it must not write over B's row."""
    store.add(_job(next_run_at=None))
    a = store.claim_for_run("j1")
    execute_retry(store.db_path, "UPDATE cron_jobs SET lease_until=? WHERE id='j1'",
                  (time.time() - 5,))
    assert store.reclaim_stale_running(under_lock=False) == 1
    b = store.claim_for_run("j1")
    assert b is not None and b.claim_lease != a.claim_lease
    store.update_after_run("j1", last_run_at=datetime(2026, 6, 6, 12), next_run_at=None,
                           status="done", expected=a)
    row = store.get("j1")
    assert row.status == "running"                       # B still holds it
    store.update_after_run("j1", last_run_at=datetime(2026, 6, 6, 12), next_run_at=None,
                           status="done", expected=b)
    assert store.get("j1").status == "done"
