"""h_goals_view.py — the REPL's ``/goals`` view.

113 W4 item 3 (D-1): the REPL renders the board with the ONE renderer every
seat uses, ``core.goal_board_render.render_board`` — the chat apps' ``/goals``
(``surfaces.telegram.status_ops._goals_reply``) calls it too, so one verb gives
one text. It reads ``status_counts`` / ``list_recent`` (never ``board.list``,
the dispatcher's order) and names an unreadable board UNKNOWN, never empty.

Its own module because ``handlers.py`` sits at its size ratchet.

A READ never creates the store: a missing ``goals.db`` reads as the empty board.
"""
from __future__ import annotations

import os


def _board_path() -> str:
    """The goals DB under the ONE owner/admin home seam (C2)."""
    from cli._admin_home import admin_data_dir
    from core.runtime_paths import goals_db_path
    return goals_db_path(admin_data_dir(write=False))


def goals_view(user_id: str) -> str:
    """The rendered ``/goals`` body for *user_id*."""
    from core.goal_board_render import NO_GOALS_YET, render_board
    path = _board_path()
    if not os.path.exists(path):
        return NO_GOALS_YET
    try:
        from agents.task.goals.board import GoalBoard
        board = GoalBoard(path)
    except Exception as exc:  # unreadable/locked store — never a confident zero
        return (f"Goal board: unavailable ({type(exc).__name__}: {str(exc)[:120]}) "
                "— this is UNKNOWN, not an empty board.")
    return render_board(board, user_id=user_id)
