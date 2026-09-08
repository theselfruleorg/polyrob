"""Telegram ``/goals`` reads the board as a VIEW, not through the dispatcher's
``priority DESC LIMIT`` window (2026-08-29 C7 / C11).

``_goals_reply`` counted statuses over ``board.list(limit=1000)`` and would have
hit the same oldest-rows eviction the agent's ``goal_list`` hit, at 1000 rows.
Counts come from SQL over every row; the recent list is newest-first.
"""
from agents.task.goals.board import GoalBoard
from surfaces.telegram.harness import _goals_reply


def _done(board, title, priority=5):
    g = board.create(user_id="rob", title=title, priority=priority, force=True)
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_success(g.id, result="ok")
    return g


def test_goals_reply_counts_every_row_and_lists_open_newest_first(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    for i in range(1200):
        _done(board, f"finished {i}")
    older = board.create(user_id="rob", title="older ready", priority=9, force=True)
    newer = board.create(user_id="rob", title="Treasury: refresh the watchlist",
                         priority=2, force=True)

    text = _goals_reply("rob", str(tmp_path), board=board)

    assert "1202 goal(s)" in text
    assert "done=1200" in text and "ready=2" in text
    assert text.index(newer.id[:8]) < text.index(older.id[:8])


def test_goals_reply_empty_board(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    assert _goals_reply("rob", str(tmp_path), board=board) == "No goals yet."
