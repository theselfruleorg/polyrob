"""Wake change-gate: a change-gated cron review tick skips the paid model call
when nothing observable changed since the last SUCCESSFUL run.

Fail-open contract: any fingerprint-source error means "changed" (never block a
legitimate wake). A failed run never establishes a skippable baseline — a
persistently-failing job always retries instead of being silently swallowed.
"""
import asyncio
import os

import pytest

from agents.task.constants import AutonomyConfig
from cron.jobs import CronJob
from cron.wake_gate import (
    WakeGateStore,
    compute_wake_fingerprint,
    record_wake_outcome,
    should_skip_wake,
)


@pytest.fixture()
def data_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("WAKE_CHANGE_GATE", "AUTONOMY_POSTURE", "POLYROB_LOCAL", "ROB_LOCAL"):
        monkeypatch.delenv(var, raising=False)


def _board(data_dir):
    from agents.task.goals.board import GoalBoard
    return GoalBoard(os.path.join(data_dir, "goals.db"))


# --- flag / posture semantics ------------------------------------------------

def test_flag_default_off_in_silent_and_owner_visible(monkeypatch):
    assert AutonomyConfig.wake_change_gate() is False
    monkeypatch.setenv("AUTONOMY_POSTURE", "owner-visible")
    assert AutonomyConfig.wake_change_gate() is False


def test_flag_on_under_full_posture_and_env_wins(monkeypatch):
    monkeypatch.setenv("AUTONOMY_POSTURE", "full")
    assert AutonomyConfig.wake_change_gate() is True
    monkeypatch.setenv("WAKE_CHANGE_GATE", "false")
    assert AutonomyConfig.wake_change_gate() is False


# --- fingerprint -------------------------------------------------------------

def test_fingerprint_stable_when_nothing_changes(data_dir):
    _board(data_dir)  # create empty goals.db
    fp1 = compute_wake_fingerprint("u1", data_dir=data_dir)
    fp2 = compute_wake_fingerprint("u1", data_dir=data_dir)
    assert fp1 == fp2


def test_fingerprint_changes_on_goal_board_mutation(data_dir):
    board = _board(data_dir)
    fp1 = compute_wake_fingerprint("u1", data_dir=data_dir)
    board.create(user_id="u1", title="new goal", payload={})
    fp2 = compute_wake_fingerprint("u1", data_dir=data_dir)
    assert fp1 != fp2


def test_fingerprint_tenant_scoped(data_dir):
    board = _board(data_dir)
    fp_before = compute_wake_fingerprint("u1", data_dir=data_dir)
    board.create(user_id="OTHER", title="not mine", payload={})
    fp_after = compute_wake_fingerprint("u1", data_dir=data_dir)
    assert fp_before == fp_after


def test_fingerprint_fail_open_on_missing_dir():
    # nonexistent dir must still return a value (neutral), never raise
    fp = compute_wake_fingerprint("u1", data_dir="/nonexistent/nowhere")
    assert isinstance(fp, str) and fp


# --- store -------------------------------------------------------------------

def test_store_roundtrip_and_first_seen(data_dir):
    store = WakeGateStore(os.path.join(data_dir, "cron.db"))
    assert store.get("job1") is None
    store.put("job1", "u1", "abc")
    assert store.get("job1") == {"fingerprint": "abc", "ok": 1}
    store.put("job1", "u1", "def", ok=False)
    assert store.get("job1") == {"fingerprint": "def", "ok": 0}


# --- should_skip_wake orchestration -------------------------------------------

def _job(payload=None):
    return CronJob(id="j1", user_id="u1", task="review the board",
                   schedule_spec="30m", next_run_at=None,
                   payload=payload or {})


def test_not_gated_job_never_skips(data_dir, monkeypatch):
    monkeypatch.setenv("WAKE_CHANGE_GATE", "true")
    assert should_skip_wake(_job(payload={}), data_dir=data_dir) is False


def test_gated_job_runs_first_tick_then_skips_until_change(data_dir, monkeypatch):
    monkeypatch.setenv("WAKE_CHANGE_GATE", "true")
    board = _board(data_dir)
    job = _job(payload={"change_gated": True})
    # first tick: no baseline -> run; a successful run records the baseline
    assert should_skip_wake(job, data_dir=data_dir) is False
    record_wake_outcome(job, data_dir=data_dir, ok=True)
    # second tick, nothing changed since the successful run -> skip
    assert should_skip_wake(job, data_dir=data_dir) is True
    # board mutates -> run again
    board.create(user_id="u1", title="g", payload={})
    assert should_skip_wake(job, data_dir=data_dir) is False
    record_wake_outcome(job, data_dir=data_dir, ok=True)
    assert should_skip_wake(job, data_dir=data_dir) is True


def test_failed_run_never_establishes_skippable_baseline(data_dir, monkeypatch):
    monkeypatch.setenv("WAKE_CHANGE_GATE", "true")
    _board(data_dir)
    job = _job(payload={"change_gated": True})
    assert should_skip_wake(job, data_dir=data_dir) is False
    record_wake_outcome(job, data_dir=data_dir, ok=False)
    # nothing changed, but the last run FAILED -> must retry, never skip
    assert should_skip_wake(job, data_dir=data_dir) is False
    # a later successful run restores skipping
    record_wake_outcome(job, data_dir=data_dir, ok=True)
    assert should_skip_wake(job, data_dir=data_dir) is True


def test_delivery_jobs_never_gated(data_dir, monkeypatch):
    monkeypatch.setenv("WAKE_CHANGE_GATE", "true")
    job = _job(payload={"change_gated": True, "deliver": "telegram"})
    assert should_skip_wake(job, data_dir=data_dir) is False
    assert should_skip_wake(job, data_dir=data_dir) is False


def test_global_flag_off_disables_gate(data_dir):
    job = _job(payload={"change_gated": True})
    assert should_skip_wake(job, data_dir=data_dir) is False
    assert should_skip_wake(job, data_dir=data_dir) is False


# --- runner integration --------------------------------------------------------

def test_runner_retries_failing_gated_job(data_dir, monkeypatch):
    # A failing job mutates no observable state — with a naive advance-on-run
    # baseline it would fingerprint as "unchanged" and be silently skipped
    # forever. The outcome-tagged baseline makes it retry every tick.
    monkeypatch.setenv("WAKE_CHANGE_GATE", "true")
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    from cron.runner import make_agent_runner

    calls = {"n": 0}

    class FailingAgent:
        async def create_session(self, **kw):
            calls["n"] += 1
            raise RuntimeError("session setup broken")

    runner = make_agent_runner(FailingAgent(), data_dir=data_dir)
    job = _job(payload={"change_gated": True})
    assert asyncio.run(runner(job)) is False
    assert asyncio.run(runner(job)) is False
    assert calls["n"] == 2  # retried, NOT skipped as no_change
