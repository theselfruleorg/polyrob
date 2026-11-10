"""A failed open-ask read holds every goal (fail-closed); ``/status`` names it as a CRIT
while the record is fresh and goals are ready, so a frozen board never reads healthy."""
import time

from agents.task.goals.board import GoalBoard
from core.event_kinds import GOAL_HOLD_UNREADABLE
from core.status_types import SEVERITY_CRIT, Section
from core.status_work import _work_section


def _tele(rows):
    return Section(name="telemetry", data={"rows": rows, "counts": {}})


def _row(ts, error="RuntimeError: db gone", ready=2):
    return {"ts": ts, "kind": GOAL_HOLD_UNREADABLE, "user_id": "", "attrs":
            {"error": error, "ready": ready}}


def _items(sec):
    return [h for h in sec.health if h.key == "goals_held_unreadable"]


def test_fresh_unreadable_record_with_ready_goals_is_a_crit(tmp_path):
    db = str(tmp_path / "g.db")
    b = GoalBoard(db)
    b.create(user_id="alice", title="Recap")
    now = time.time()
    sec = _work_section("alice", db, _tele([_row(now - 120)]), now)
    [item] = _items(sec)
    assert item.severity == SEVERITY_CRIT
    assert "no goal runs" in item.text and "db gone" in item.text
    assert "1 ready goal(s) wait" in item.text
    assert item.remedy


def test_stale_record_or_no_ready_goal_is_no_item(tmp_path):
    db = str(tmp_path / "g.db")
    b = GoalBoard(db)
    now = time.time()
    assert _items(_work_section("alice", db, _tele([_row(now - 60)]), now)) == []
    b.create(user_id="alice", title="Recap")
    assert _items(_work_section("alice", db, _tele([_row(now - 1000)]), now)) == []
