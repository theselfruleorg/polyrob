"""C15 / C16 / E17 (2026-09-21) — the REPL's ``/goals`` view.

⚠️ ``GoalBoard.list`` is the DISPATCHER's order (``priority DESC, created_at
ASC LIMIT``). As a VIEW it is wrong: on the 409-row prod board it showed the
OLDEST rows and zero live legs, and the agent told the owner it had no trading
goals while ten clean cycles had run. The view reads ``list_recent`` +
``status_counts``.
"""
import pytest

from cli.ui.commands.h_goals_view import goals_view, read_board


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


#: Distinct titles: the board refuses a NEAR-duplicate, so "goal 1"/"goal 11"
#: cannot both be created.
_WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
          "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
          "oscar")


def _seed(home, n, *, user_id="u1"):
    from agents.task.goals.board import GoalBoard
    board = GoalBoard(str(home / "goals.db"))
    ids = []
    for i in range(n):
        g = board.create(title=_WORDS[i], user_id=user_id,
                         priority=1 if i else 5)
        ids.append(g.id)
    return board, ids


def test_missing_store_reads_as_empty_and_is_not_created(_home):
    rows, counts, reason = read_board("u1")
    assert rows == [] and counts == {} and reason is None
    assert not (_home / "goals.db").exists()


def test_empty_state_names_a_verb_not_a_flag(_home):
    out = goals_view("u1")
    assert "no goals yet" in out
    # The remedy must name something this seat can RUN. `/goal create` is not a
    # `/goal` subcommand (it steers an existing goal), so it answered with a
    # usage line — the same "remedy names a command the seat cannot run" defect
    # C5 removed from `/approve`.
    assert "/goal create" not in out
    assert "/trade" in out
    assert "polyrob goals create" in out
    assert "GOALS_ENABLED" not in out


def test_view_is_newest_first_not_the_dispatch_order(_home):
    """The NEWEST rows are shown even when an older row outranks them.

    ``_seed`` gives the FIRST goal priority 5 and every later goal priority 1,
    so ``board.list``'s order would put the first one first and, at the view's
    limit, could hide every newer row. ``list_recent`` shows the newest.
    """
    _seed(_home, 15, user_id="u1")
    rows, counts, reason = read_board("u1", limit=5)
    assert reason is None
    assert len(rows) == 5
    assert [r.title for r in rows] == list(reversed(_WORDS[10:15]))
    assert _WORDS[0] not in [r.title for r in rows]   # the priority-5 row


def test_counts_cover_every_row_not_the_window(_home):
    _seed(_home, 15, user_id="u1")
    out = goals_view("u1", limit=5)
    assert "15 goal(s) on the board" in out
    assert "newest 5 shown" in out


def test_view_is_tenant_scoped(_home):
    _seed(_home, 3, user_id="u1")
    assert "no goals yet" in goals_view("u2")


def test_unreadable_store_is_unknown_never_empty(_home, monkeypatch):
    (_home / "goals.db").write_text("not a database")
    rows, counts, reason = read_board("u1")
    assert rows is None
    assert reason
    out = goals_view("u1")
    assert "unavailable" in out
    assert "UNKNOWN, not" in out
    assert "no goals" not in out
