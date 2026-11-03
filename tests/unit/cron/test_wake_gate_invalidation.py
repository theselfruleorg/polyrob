"""098 MO-11: an owner edit, a resume or a cancel invalidates a job's wake baseline."""
import os
from datetime import datetime

import pytest

from cron.jobs import CronJob, CronJobStore
from cron.wake_gate import WakeGateStore, record_wake_outcome, should_skip_wake


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    for var in ("AUTONOMY_POSTURE", "POLYROB_LOCAL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WAKE_CHANGE_GATE", "true")


@pytest.fixture
def setup(tmp_path):
    data_dir = str(tmp_path)
    store = CronJobStore(os.path.join(data_dir, "cron.db"))
    store.add(CronJob(id="j1", user_id="u1", task="summarise X", schedule_spec="30m",
                      next_run_at=datetime(2026, 1, 1), payload={"change_gated": True}))
    job = store.get("j1")
    assert should_skip_wake(job, data_dir=data_dir) is False
    record_wake_outcome(job, data_dir=data_dir, ok=True)
    assert should_skip_wake(store.get("j1"), data_dir=data_dir) is True
    return data_dir, store


def test_set_task_unskips_gated_job(setup):
    data_dir, store = setup
    assert store.set_task("j1", "summarise Y")
    assert should_skip_wake(store.get("j1"), data_dir=data_dir) is False


def test_run_notes_do_not_unskip(setup):
    data_dir, store = setup
    store.record_outcome("j1", "ok", datetime(2026, 1, 2))
    assert should_skip_wake(store.get("j1"), data_dir=data_dir) is True


def test_resume_runs_once_even_if_state_unchanged(setup):
    data_dir, store = setup
    assert store.pause("j1", user_id="u1", note={"by": "owner"})
    assert store.resume("j1", user_id="u1", next_run_at=datetime(2026, 1, 3))
    assert should_skip_wake(store.get("j1"), data_dir=data_dir) is False


def test_cancel_deletes_baseline(setup):
    data_dir, store = setup
    assert store.cancel("j1")
    assert WakeGateStore(store.db_path).get("j1") is None


def test_prune_deletes_orphan_baselines(setup):
    data_dir, store = setup
    store.cancel("j1")
    WakeGateStore(store.db_path).put("j1", "u1", "fp")
    assert store.prune_cancelled(older_than_days=0, now=datetime(2030, 1, 1)) == 1
    assert WakeGateStore(store.db_path).get("j1") is None
