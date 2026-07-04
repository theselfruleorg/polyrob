import inspect
from datetime import datetime

import cron.jobs as jobs_mod


def test_cron_jobs_does_not_reimplement_sqlite_retry():
    """Guard: cron/jobs must not hand-roll jitter; must import shared helper."""
    src = inspect.getsource(jobs_mod)
    assert "random.uniform(0.02, 0.15)" not in src, "cron/jobs must not hand-roll jitter"
    assert "from core.sqlite_util import" in src or "import core.sqlite_util" in src, (
        "cron/jobs must use the shared core.sqlite_util helper"
    )


def test_cron_store_roundtrips(tmp_path):
    """Verify CronJobStore still works after refactoring."""
    from cron.jobs import CronJob, CronJobStore

    store = CronJobStore(str(tmp_path / "cron.db"))
    job = CronJob(
        id="j1",
        task="t",
        schedule_spec="30m",
        user_id="u1",
        next_run_at=datetime(2026, 1, 1),
        created_at=datetime(2026, 1, 1),
    )
    store.add(job)
    assert store.get("j1").task == "t"
    assert store.cancel("j1") is True
