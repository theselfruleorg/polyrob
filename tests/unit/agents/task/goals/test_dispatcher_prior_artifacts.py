"""The dispatcher only offers a retry artifacts that are STILL there.

Listing the ledger is not enough: after the 2026-08-17 wipe every row still
existed while the files did not. Telling a retry to "continue from" a deleted
file is the same lie in a new place, so each row is verified before it is
offered.
"""
import pytest

from agents.task.goals.board import Goal
from agents.task.goals.dispatcher import GoalDispatcher
from core.artifacts import get_artifact_ledger


def test_only_surviving_artifacts_are_offered(tmp_path):
    alive = tmp_path / "kept.md"
    alive.write_text("still here")
    gone = tmp_path / "wiped.md"
    gone.write_text("about to be deleted")

    ledger = get_artifact_ledger()
    ledger.record("rob", str(alive), session_id="s1", goal_id="g1")
    ledger.record("rob", str(gone), session_id="s1", goal_id="g1")
    gone.unlink()

    goal = Goal(id="g1", user_id="rob", title="x402 Round 4")

    assert GoalDispatcher._prior_artifacts(goal) == [("kept.md", 10)]


def test_an_altered_artifact_is_not_offered(tmp_path):
    p = tmp_path / "changed.md"
    p.write_text("original")
    get_artifact_ledger().record("rob", str(p), session_id="s1", goal_id="g1")
    p.write_text("rewritten by something else")

    goal = Goal(id="g1", user_id="rob", title="x402 Round 4")

    assert GoalDispatcher._prior_artifacts(goal) == []


def test_no_artifacts_returns_empty(tmp_path):
    goal = Goal(id="g-none", user_id="rob", title="fresh goal")
    assert GoalDispatcher._prior_artifacts(goal) == []
