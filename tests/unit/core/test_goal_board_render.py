"""`/goals` must show the owner WHY nothing is moving, not just how many rows.

2026-09-08 the board looked like this to the agent and the owner:

    a66f5f92 [ready]    Treasury: refresh the watchlist
    2dee2891 [waiting]  manage positions / take a screened entry
    709ec00f [waiting]  publish the track record

Three unrelated-looking lines, two of them the bare word "waiting". Nothing
said goal 2 was parked because goal 1 had not run, though `goal_edges` holds
exactly that. The owner asked "so the trading goal is running?" and could not
tell from the surface -- which is the complaint.
"""
import types

from core.goal_board_render import render_board


def _g(gid, status, title, **kw):
    return types.SimpleNamespace(id=gid, status=status, title=title,
                                 kind="goal", priority=kw.get("priority", 1))


class _Board:
    """Just the read surface render_board is allowed to use."""
    def __init__(self, goals, edges=None):
        self._goals = {g.id: g for g in goals}
        self._edges = edges or {}

    def status_counts(self, user_id=None):
        out = {}
        for g in self._goals.values():
            out[g.status] = out.get(g.status, 0) + 1
        return out

    def list_recent(self, user_id=None, statuses=None, limit=20):
        return [g for g in self._goals.values()
                if statuses is None or g.status in statuses][:limit]

    def dependencies(self, gid):
        return self._edges.get(gid, [])

    def get(self, gid, user_id=None):
        return self._goals.get(gid)


CHAIN = _Board(
    [_g("a66f5f92", "ready", "Treasury: refresh the watchlist"),
     _g("2dee2891", "waiting", "Treasury: manage open positions and take an entry"),
     _g("709ec00f", "waiting", "Treasury: publish the track record")],
    {"2dee2891": ["a66f5f92"], "709ec00f": ["2dee2891"]})


class TestTheWaitingEdge:
    def test_a_waiting_goal_names_what_it_waits_on(self):
        out = render_board(CHAIN, user_id="u")
        assert "a66f5f92" in out
        assert "waiting on" in out.lower(), (
            "'waiting' alone tells the owner nothing; the edge is the answer")

    def test_the_blocking_goal_is_named_by_title_not_only_id(self):
        """An 8-char hex id is not something an owner can reason about."""
        out = render_board(CHAIN, user_id="u")
        assert "refresh the watchlist" in out


class TestWhatLeads:
    def test_running_work_leads(self):
        b = _Board([_g("aaaaaaaa", "ready", "queued thing"),
                    _g("bbbbbbbb", "running", "the live one")])
        out = render_board(b, user_id="u")
        assert out.index("the live one") < out.index("queued thing")

    def test_a_goal_blocked_on_the_owner_is_surfaced(self):
        b = _Board([_g("cccccccc", "blocked", "needs your approval"),
                    _g("dddddddd", "ready", "ordinary queued work")])
        out = render_board(b, user_id="u")
        assert "blocked" in out.lower()
        assert out.index("needs your approval") < out.index("ordinary queued work")


class TestHonesty:
    def test_counts_cover_every_row_not_the_shown_window(self):
        b = _Board([_g(f"{i:08x}", "ready", f"g{i}") for i in range(40)])
        out = render_board(b, user_id="u")
        assert "40" in out, "the owner must see the true total, not the window size"

    def test_an_empty_board_says_so(self):
        assert "no goals" in render_board(_Board([]), user_id="u").lower()

    def test_a_broken_edge_read_never_takes_the_view_down(self):
        """A board view that raises is worse than one without edges."""
        class _Angry(_Board):
            def dependencies(self, gid):
                raise RuntimeError("db locked")
        out = render_board(_Angry([_g("eeeeeeee", "waiting", "x")]), user_id="u")
        assert "x" in out

    def test_the_render_names_no_action_the_owner_cannot_take(self):
        from core.owner_remedy import unknown_owner_actions
        assert unknown_owner_actions(render_board(CHAIN, user_id="u")) == []
