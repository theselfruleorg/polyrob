"""U1 phase 2: the cron job store lives in ``core.cron_store``; it imports nothing
above core, and the old ``cron.jobs`` path is the same objects."""
import ast
import pathlib
from datetime import datetime

REPO = pathlib.Path(__file__).resolve().parents[3]


def test_the_store_imports_nothing_above_core():
    tree = ast.parse((REPO / "core/cron_store.py").read_text())
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            assert n.module.split(".")[0] in {"core", "dataclasses", "datetime", "typing",
                                              "types", "__future__"}, n.module
        if isinstance(n, ast.Import):
            for a in n.names:
                assert not a.name.startswith(("cron", "tools", "agents", "modules")), a.name


def test_old_paths_are_the_same_objects():
    import core.cron_store as cs
    import cron.jobs as jobs
    import cron.scheduler as sch
    assert jobs.CronJobStore is cs.CronJobStore and jobs.CronJob is cs.CronJob
    assert jobs._CLEARED_STORES is cs._CLEARED_STORES
    assert sch.RUN_CAP_CEILING_SECONDS == cs.RUN_CAP_CEILING_SECONDS == 1800
    import importlib.util
    assert importlib.util.find_spec("cron.job_machine") is None   # the shim is gone


def test_a_cancelled_job_takes_its_side_rows(tmp_path):
    """The side rows go with the job with no cron module imported by the store."""
    from core.cron_store import CronJob, CronJobStore
    from core.sqlite_util import execute_retry
    db = str(tmp_path / "cron.db")
    s = CronJobStore(db)
    s.add(CronJob(id="j1", task="t", schedule_spec="30m", user_id="u",
                  next_run_at=datetime(2026, 1, 1)))
    execute_retry(db, "CREATE TABLE wake_gate (job_id TEXT)")
    execute_retry(db, "CREATE TABLE remedy_streak (job_id TEXT)")
    execute_retry(db, "INSERT INTO wake_gate VALUES ('j1')")
    execute_retry(db, "INSERT INTO remedy_streak VALUES ('j1')")
    assert s.cancel("j1") is True
    assert execute_retry(db, "SELECT COUNT(*) AS n FROM wake_gate", fetch="one")[0] == 0
    assert execute_retry(db, "SELECT COUNT(*) AS n FROM remedy_streak", fetch="one")[0] == 0
