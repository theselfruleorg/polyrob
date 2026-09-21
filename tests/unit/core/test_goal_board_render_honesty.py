"""D7/D8: an unreadable board is not an empty one, and a read fault is not a
diagnosis.

`render_board` is the ONE board view every owner seat renders. Two of its
failure modes produced the most reassuring sentence available over a store
nobody could open:

* a `status_counts` fault became `counts = {}` and answered **"No goals yet."**
* a `dependencies` fault became `[]`, which the renderer reports as
  **"stranded"** — a specific, actionable diagnosis meaning "every prerequisite
  is done, the janitor will pick it up". Nothing had been read at all.
"""
import types

from core.goal_board_render import render_board


def _goal(gid, status="waiting", title="a goal"):
    return types.SimpleNamespace(id=gid, status=status, title=title, kind="goal")


class _Board:
    """A board whose three reads can each be made to fail independently."""

    def __init__(self, counts=None, rows=(), edges=None, *,
                 counts_raises=False, recent_raises=False, deps_raises=False,
                 get_raises=False):
        self._counts = counts or {}
        self._rows = list(rows)
        self._edges = edges or {}
        self._counts_raises = counts_raises
        self._recent_raises = recent_raises
        self._deps_raises = deps_raises
        self._get_raises = get_raises

    def status_counts(self, user_id=None):
        if self._counts_raises:
            raise RuntimeError("goals.db is locked")
        return self._counts

    def list_recent(self, user_id=None, statuses=None, limit=30):
        if self._recent_raises:
            raise RuntimeError("goals.db is locked")
        return self._rows

    def list(self, *a, **kw):
        raise AssertionError("board.list is the dispatcher order, never a view")

    def dependencies(self, gid):
        if self._deps_raises:
            raise RuntimeError("goal_edges unreadable")
        return self._edges.get(gid, [])

    def get(self, gid, user_id=None):
        if self._get_raises:
            raise RuntimeError("row unreadable")
        return next((g for g in self._rows if g.id == gid), None)


def test_an_unreadable_board_is_unavailable_not_empty():
    out = render_board(_Board(counts_raises=True))
    assert "unavailable" in out
    assert "UNKNOWN, not an empty board" in out
    assert "No goals yet" not in out


def test_a_genuinely_empty_board_still_says_so():
    """The honest empty state must survive the fix, or the fix is a regression
    for the common case."""
    assert render_board(_Board(counts={})) == "No goals yet."


def test_unreadable_open_rows_keep_the_counts_and_name_the_fault():
    out = render_board(_Board(counts={"ready": 3}, recent_raises=True))
    assert "3 goal(s)" in out
    assert "unavailable" in out
    assert "the counts above are still true" in out


def test_an_unreadable_edge_list_is_never_reported_as_stranded():
    board = _Board(counts={"waiting": 1}, rows=[_goal("aaaa1111")],
                   deps_raises=True)
    out = render_board(board)
    assert "no unfinished prerequisite" not in out   # the STRANDED diagnosis
    assert "prerequisites are unavailable" in out
    assert "cannot tell whether it is queued or stranded" in out


def test_a_genuinely_stranded_row_still_says_stranded():
    board = _Board(counts={"waiting": 1}, rows=[_goal("aaaa1111")], edges={})
    out = render_board(board)
    assert "no unfinished prerequisite" in out and "stranded" in out


def test_unreadable_prerequisite_rows_are_not_no_prerequisites():
    """The subtler half: the edge LIST read fine, every row it named did not."""
    board = _Board(counts={"waiting": 1}, rows=[_goal("aaaa1111")],
                   edges={"aaaa1111": ["bbbb2222"]}, get_raises=True)
    out = render_board(board)
    assert "no unfinished prerequisite" not in out   # the STRANDED diagnosis
    assert "unreadable" in out


def test_a_waiting_row_names_its_blocking_prerequisite():
    prereq = _goal("bbbb2222", status="ready", title="run me first")
    board = _Board(counts={"waiting": 1, "ready": 1},
                   rows=[_goal("aaaa1111"), prereq],
                   edges={"aaaa1111": ["bbbb2222"]})
    out = render_board(board)
    assert "waiting on bbbb2222" in out and "run me first" in out
