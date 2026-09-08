"""/goal objective — the owner can see and steer streams from a phone."""
import pytest

from agents.task.goals.board import GoalBoard
from surfaces.telegram import owner_ops


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_objective_list_shows_streams_and_live_counts(board, tmp_path):
    o = board.create_objective(user_id="rob", title="Ship weekly video", force=True)
    board.create(user_id="rob", title="child", parent_id=o.id, force=True)
    out = owner_ops.goal_reply("rob", str(tmp_path), ["objective", "list"], board=board)
    assert "Ship weekly video" in out
    assert "1 live" in out


def test_objective_pause_and_activate(board, tmp_path):
    o = board.create_objective(user_id="rob", title="Ship weekly video", force=True)
    out = owner_ops.goal_reply("rob", str(tmp_path), ["objective", "pause", o.id[:8]],
                               board=board)
    assert "paused" in out
    assert board.get(o.id).status == "paused"
    owner_ops.goal_reply("rob", str(tmp_path), ["objective", "activate", o.id[:8]],
                         board=board)
    assert board.get(o.id).status == "active"


def test_objective_usage_when_the_subverb_is_unknown(board, tmp_path):
    out = owner_ops.goal_reply("rob", str(tmp_path), ["objective", "frobnicate"],
                               board=board)
    assert "Usage: /goal objective" in out
