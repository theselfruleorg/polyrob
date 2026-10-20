"""Review 2026-10-15 rec. 2 — the agent's ``<operations>`` block.

Live jobs, open goals, the /pause record and the owner's stops, rendered from
the status SSOT's own ``work`` / ``loops`` builders. A store that cannot be
read says "could not read", never "none"; the block stays within 25 lines.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from core.status_render import render_operations_block
from core.status_snapshot import build_operations_sections

OWNER = "rob"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("CRON_ENABLED", "true")
    for k in ("AUTONOMY_PAUSE", "AUTONOMY_PAUSED"):
        monkeypatch.delenv(k, raising=False)
    return str(tmp_path)


def _board(d):
    from agents.task.goals.board import GoalBoard
    return GoalBoard(os.path.join(d, "goals.db"))


def _cron(d, n=2, paused_id=None):
    from cron.jobs import CronJob, CronJobStore
    store = CronJobStore(os.path.join(d, "cron.db"))
    now = datetime.now(timezone.utc)
    for i in range(n):
        store.add(CronJob(id=f"job{i:02d}", task=f"Job number {i}\nsecond line",
                          schedule_spec="every day 09:00", user_id=OWNER,
                          next_run_at=now + timedelta(hours=i + 1), enabled=True,
                          created_at=now))
    store.add(CronJob(id="gone", task="Cancelled job", schedule_spec="30m", user_id=OWNER,
                      next_run_at=None, enabled=False, created_at=now))
    if paused_id:
        store.pause(paused_id, user_id=OWNER, note={"by": "owner"})
    return store


def _block(d):
    return render_operations_block(build_operations_sections(OWNER, data_dir=d))


def test_block_lists_jobs_goals_pause_and_stops(home):
    from core import autonomy_control, goal_suppressions
    _cron(home, n=2, paused_id="job01")
    board = _board(home)
    g = board.create(user_id=OWNER, title="Watch the ETH price")
    board.create(user_id="someone-else", title="Not rob's goal")
    goal_suppressions.suppress(os.path.join(home, "goals.db"), user_id=OWNER,
                               scope="title", value="Buy back ROB", label="Buy back ROB")
    autonomy_control.pause(home, scopes=("trading",), set_by="owner", via="telegram")
    text = _block(home)
    lines = text.splitlines()
    assert lines[0].startswith("Live harness state as of ")
    assert "Pause: ON (trading)" in text
    assert 'job00 "Job number 0" · every day 09:00 · next ' in text
    assert 'job01 "Job number 1"' in text and "PAUSED" in text
    assert "gone" not in text  # a cancelled job is not active
    assert f'{g.id} "Watch the ETH price"' in text
    assert "Not rob's goal" not in text  # tenant-scoped
    assert "- title: Buy back ROB since " in text


def test_every_stop_scope_renders_generically(home):
    from core import goal_suppressions
    _board(home)
    db = os.path.join(home, "goals.db")
    for scope in goal_suppressions.SCOPES:
        goal_suppressions.suppress(db, user_id=OWNER, scope=scope, value=f"lane-{scope}",
                                   label=f"label-{scope}")
    text = _block(home)
    for scope in goal_suppressions.SCOPES:
        assert f"- {scope}: label-{scope} since " in text


def test_empty_state_says_none(home):
    _cron(home, n=0)
    _board(home)
    text = _block(home)
    assert "Pause: none" in text
    assert "Jobs: none" in text and "Open goals: none" in text and "Owner stops: none" in text


def test_unreadable_stores_say_could_not_read_never_none(home):
    # No goals.db and no cron.db at all: both are UNKNOWN, not empty.
    text = _block(home)
    assert "Jobs: could not read" in text
    assert "Open goals: could not read" in text
    assert "Owner stops: could not read" in text
    assert "Jobs: none" not in text and "Open goals: none" not in text


def test_unreadable_stop_store_is_named(home, monkeypatch):
    from core import goal_suppressions
    _cron(home, n=1)
    _board(home)

    def boom(*a, **k):
        raise RuntimeError("disk I/O error")
    monkeypatch.setattr(goal_suppressions, "active", boom)
    text = _block(home)
    assert "Owner stops: could not read (RuntimeError: disk I/O error)" in text
    assert "Jobs (1):" in text  # the other lists still render


def test_unreadable_pause_record_is_not_none(home, monkeypatch):
    from core import autonomy_control
    _cron(home, n=0)
    _board(home)
    monkeypatch.setattr(autonomy_control, "read_state",
                        lambda *a, **k: autonomy_control.unreadable_state("bad json"))
    text = _block(home)
    assert "Pause: could not read the pause record" in text


def test_empty_tenant_is_unknown(home):
    text = render_operations_block(build_operations_sections("", data_dir=home))
    assert "could not read (no tenant" in text


def test_block_is_capped(home):
    from core import goal_suppressions
    _cron(home, n=20)
    board = _board(home)
    for i in range(20):
        board.create(user_id=OWNER, title=f"Goal {i}", force=True)
    for i in range(20):
        goal_suppressions.suppress(os.path.join(home, "goals.db"), user_id=OWNER,
                                   scope="title", value=f"Stop {i}")
    lines = _block(home).splitlines()
    assert len(lines) <= 25
    text = "\n".join(lines)
    assert "+14 more, see /jobs" in text
    assert "+14 more, see /goals" in text
    assert "+15 more, see /goals and /jobs" in text


def test_full_snapshot_carries_the_same_data(home):
    """One source: the full snapshot's sections render the same block."""
    from core.status_snapshot import build_status_snapshot
    _cron(home, n=1)
    _board(home).create(user_id=OWNER, title="One goal")
    snap = build_status_snapshot(OWNER, data_dir=home, include_money=False)
    full = render_operations_block(snap).splitlines()[1:]
    narrow = _block(home).splitlines()[1:]
    assert full == narrow
