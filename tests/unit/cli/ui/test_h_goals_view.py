"""The REPL's ``/goals`` view (C15/C16/E17, 2026-09-21; 113 W4 item 3).

The REPL renders the board with the ONE renderer every seat uses
(``core.goal_board_render.render_board``). ⚠️ ``GoalBoard.list`` is the
DISPATCHER's order (``priority DESC``): as a VIEW it showed the OLDEST rows of
the 409-row prod board. The renderer reads ``list_recent`` + ``status_counts``.
"""
import pytest

from cli.ui.commands.h_goals_view import goals_view


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


#: Distinct titles: the board refuses a NEAR-duplicate.
_WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
          "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
          "oscar")


def _seed(home, n, *, user_id="u1"):
    from agents.task.goals.board import GoalBoard
    board = GoalBoard(str(home / "goals.db"))
    for i in range(n):
        board.create(title=_WORDS[i], user_id=user_id, priority=1 if i else 5)
    return board


def test_missing_store_reads_as_empty_and_is_not_created(_home):
    out = goals_view("u1")
    assert out.startswith("No goals yet.")
    assert not (_home / "goals.db").exists()


def test_empty_state_names_a_verb_not_a_flag(_home):
    out = goals_view("u1")
    assert "/goal create" not in out
    assert "/goal new" in out
    assert "GOALS_ENABLED" not in out


def test_the_repl_and_the_chat_apps_render_the_same_board(_home):
    from surfaces.telegram.status_ops import _goals_reply
    _seed(_home, 3)
    assert goals_view("u1") == _goals_reply("u1", str(_home))


def test_view_is_newest_first_and_counts_every_row(_home):
    _seed(_home, 15)
    out = goals_view("u1")
    assert "15 goal(s)" in out
    assert "oscar" in out                 # the newest row is shown
    assert "alpha" not in out             # the oldest (priority-5) row is past the window
    assert "more open goal(s)" in out


def test_view_is_tenant_scoped(_home):
    _seed(_home, 3, user_id="u1")
    assert goals_view("u2").startswith("No goals yet.")


def test_unreadable_store_is_unknown_never_empty(_home):
    (_home / "goals.db").write_text("not a database")
    out = goals_view("u1")
    assert "unavailable" in out
    assert "UNKNOWN, not" in out
    assert "No goals" not in out
