"""Proposal 125 S13 (review A-7): ONE "is the agent busy" reader.

`core.agent_activity` is the reader the drain, `polyrob update` and the prod deploy
waiter (`scripts/deploy_when_idle.sh`, through the venv python) all call. The goal
statuses come from `core.goal_vocab`; an unreadable store is busy and makes the
command exit non-zero; a marker that is not a plain file is a live turn (a root
reader never follows a link and never blocks on a FIFO)."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from core import agent_activity as aa
from core import goal_vocab

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_WORKSPACE_LOCK_DIR", raising=False)
    (tmp_path / "locks").mkdir()
    return tmp_path


def _goals(home, statuses):
    con = sqlite3.connect(home / "goals.db")
    con.execute("CREATE TABLE goals (id TEXT, status TEXT)")
    con.executemany("INSERT INTO goals VALUES (?, ?)", [(f"g{i}", s) for i, s in enumerate(statuses)])
    con.commit(); con.close()


def test_busy_statuses_come_from_goal_vocab(home):
    assert aa.GOAL_BUSY_STATUSES == (goal_vocab.STATUS_RUNNING,)
    assert set(aa.GOAL_BUSY_STATUSES) <= goal_vocab.LIVE_STATUSES
    # 'claimed' is not a goal status: a row in it is not a run.
    _goals(home, ["claimed", "ready"])
    assert aa.observe(home).idle
    os.remove(home / "goals.db"); _goals(home, [goal_vocab.STATUS_RUNNING])
    assert aa.observe(home).signals["goals_running"] == 1


def test_data_home_defaults_through_runtime_paths(home):
    _goals(home, ["running"])
    assert not aa.observe().idle


def test_a_fifo_marker_is_a_live_turn_and_never_blocks(home):
    os.mkfifo(home / "locks" / "turn.active")
    act = aa.observe(home)
    assert not act.idle and act.signals["turn"] == 1


def test_a_linked_marker_is_not_followed(home, tmp_path_factory):
    other = tmp_path_factory.mktemp("o") / "m.json"
    other.write_text(json.dumps({"pid": os.getpid(), "kind": "owner_chat"}))
    os.symlink(other, home / "locks" / "turn.active")
    act = aa.observe(home)
    assert act.signals["turn"] == 1 and "cannot be" in act.describe()


def _main(home, *extra):
    return subprocess.run([sys.executable, "-m", "core.agent_activity", "--json",
                           "--data", str(home), *extra],
                          cwd=REPO, capture_output=True, text=True, timeout=60)


def test_main_json_idle_and_busy(home):
    r = _main(home)
    assert r.returncode == 0, r.stderr
    body = json.loads(r.stdout)
    assert body["idle"] is True and body["signals"]["goals_running"] == 0
    _goals(home, ["running"])
    body = json.loads(_main(home).stdout)
    assert body["idle"] is False and body["signals"]["goals_running"] == 1


def test_main_exits_non_zero_on_an_unreadable_store(home):
    (home / "cron.db").write_bytes(b"not a database")
    r = _main(home, "--imminent", "60")
    assert r.returncode == 2
    body = json.loads(r.stdout)
    assert body["idle"] is False and body["signals"]["cron_running"] == "unreadable"


def test_a_job_due_soon_is_busy_in_a_zone_east_of_utc(tmp_path, monkeypatch):
    """next_run_at is the store's naive LOCAL time: a job written through CronJobStore
    and due in 30 s is "due soon" on a box east of UTC (a UTC horizon read it idle)."""
    import time as _time
    from datetime import datetime, timedelta
    from core.cron_store import CronJob, CronJobStore
    from core.runtime_paths import cron_db_path
    if not hasattr(_time, "tzset"):
        pytest.skip("no tzset on this platform")
    monkeypatch.setenv("TZ", "Asia/Bangkok")
    _time.tzset()
    try:
        store = CronJobStore(cron_db_path(str(tmp_path)))
        store.add(CronJob(id="j1", task="t", schedule_spec="every 1h", user_id="u",
                          next_run_at=datetime.now() + timedelta(seconds=30)))
        a = aa.observe(tmp_path, cron_imminent_sec=120)
        assert a.signals["cron_imminent"] == 1 and not a.idle
    finally:
        monkeypatch.delenv("TZ")
        _time.tzset()
