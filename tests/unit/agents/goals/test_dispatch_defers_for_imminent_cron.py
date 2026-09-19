"""A board goal must not start when a cron job is due within the headroom window.

Prod evidence (2026-09-18, tick 56/57): every session shares ONE project-root
workspace (`POLYROB_PROJECT_DIR`), so a goal run marks the process busy and cron
ticks skip for the goal's whole duration — the SCOUT money rail due 13:30Z ran at
13:43:51Z, 2.5 min after a low-value ops goal ended. The serialization itself is
right (the rails share ledger files); the PRIORITY was wrong. With
`GOAL_DISPATCH_CRON_HEADROOM_SEC` > 0 the dispatcher defers a tick while any
enabled, scheduled cron job is due within that window (overdue included), so the
money rail goes first and the goal runs on the next idle tick. Default 0 = off,
byte-identical. Unreadable cron store = fail-open (dispatch).
"""
from datetime import datetime, timedelta

from agents.task.goals import dispatcher as d


def _store(tmp_path, next_run_at):
    from cron.jobs import CronJob, CronJobStore
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(CronJob(id="exit0001", task="EXIT RAIL", schedule_spec="every 2h",
                      user_id="rob", next_run_at=next_run_at))
    return store


def test_imminent_cron_job_is_named(tmp_path):
    now = datetime(2026, 9, 18, 13, 28)
    _store(tmp_path, now + timedelta(minutes=2))
    hit = d.imminent_cron_job(str(tmp_path), now, headroom_sec=600)
    assert hit is not None and "EXIT RAIL" in hit


def test_overdue_cron_job_counts_as_imminent(tmp_path):
    now = datetime(2026, 9, 18, 13, 40)
    _store(tmp_path, now - timedelta(minutes=10))
    assert d.imminent_cron_job(str(tmp_path), now, headroom_sec=600) is not None


def test_far_cron_job_does_not_defer(tmp_path):
    now = datetime(2026, 9, 18, 13, 28)
    _store(tmp_path, now + timedelta(hours=1))
    assert d.imminent_cron_job(str(tmp_path), now, headroom_sec=600) is None


def test_headroom_zero_is_off_and_never_reads_the_store(tmp_path):
    now = datetime(2026, 9, 18, 13, 28)
    assert d.imminent_cron_job(str(tmp_path), now, headroom_sec=0) is None
    assert not (tmp_path / "cron.db").exists()


def test_missing_cron_store_fails_open(tmp_path):
    now = datetime(2026, 9, 18, 13, 28)
    assert d.imminent_cron_job(str(tmp_path / "nope"), now, headroom_sec=600) is None
    assert not (tmp_path / "nope").exists()


def test_running_cron_job_defers_too(tmp_path):
    """A cron job mid-run holds the shared workspace exactly as an imminent one will
    (prod 2026-09-18 15:35Z: a goal started while the X ORIGINAL cron was running).
    Its `next_run_at` is still its (past) due time until it finishes."""
    now = datetime(2026, 9, 18, 15, 35)
    store = _store(tmp_path, now - timedelta(minutes=5))
    store.set_status("exit0001", "running")
    assert d.imminent_cron_job(str(tmp_path), now, headroom_sec=600) is not None


def test_done_or_failed_cron_job_does_not_defer(tmp_path):
    now = datetime(2026, 9, 18, 15, 35)
    store = _store(tmp_path, now - timedelta(minutes=5))
    store.set_status("exit0001", "failed")
    assert d.imminent_cron_job(str(tmp_path), now, headroom_sec=600) is None


# --- 056 D1 follow-up (2026-09-19): a money rail PRE-EMPTS, so it does not defer ---
# Prod 08:00-09:00Z after the D4 stagger: EXIT :00, SAFETY :20, WATCHER :40, SCOUT :50,
# each with a 600 s headroom, left NO minute in an even hour where a goal could start
# (two seeded goals sat `ready` for 40 min). With GOAL_YIELD_FOR_MONEY_RAIL on, a due
# money job pre-empts a running goal at the step boundary (`yield_for_rail`), so the
# headroom only needs to protect the jobs that cannot pre-empt: the ops class.

def _money_store(tmp_path, next_run_at, *, money=True):
    from cron.jobs import CronJob, CronJobStore
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(CronJob(id="exit0001", task="EXIT RAIL", schedule_spec="every 2h",
                      user_id="rob", next_run_at=next_run_at,
                      payload={"priority": "money"} if money else {}))
    return store


def test_money_job_does_not_defer_when_yield_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_YIELD_FOR_MONEY_RAIL", "true")
    now = datetime(2026, 9, 19, 8, 36)
    _money_store(tmp_path, now + timedelta(minutes=4))
    assert d.imminent_cron_job(str(tmp_path), now, headroom_sec=600) is None


def test_money_job_still_defers_when_yield_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_YIELD_FOR_MONEY_RAIL", "false")
    now = datetime(2026, 9, 19, 8, 36)
    _money_store(tmp_path, now + timedelta(minutes=4))
    assert d.imminent_cron_job(str(tmp_path), now, headroom_sec=600) is not None


def test_ops_job_still_defers_with_yield_on(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_YIELD_FOR_MONEY_RAIL", "true")
    now = datetime(2026, 9, 19, 8, 36)
    _money_store(tmp_path, now + timedelta(minutes=4), money=False)
    assert d.imminent_cron_job(str(tmp_path), now, headroom_sec=600) is not None


def test_running_money_job_still_defers_with_yield_on(tmp_path, monkeypatch):
    """Mid-run it HOLDS the workspace now — nothing to pre-empt, so wait for it."""
    monkeypatch.setenv("GOAL_YIELD_FOR_MONEY_RAIL", "true")
    now = datetime(2026, 9, 19, 8, 36)
    store = _money_store(tmp_path, now - timedelta(minutes=2))
    assert store.claim_for_run("exit0001")
    assert d.imminent_cron_job(str(tmp_path), now, headroom_sec=600) is not None
