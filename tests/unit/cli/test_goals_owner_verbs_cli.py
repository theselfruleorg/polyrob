"""080 WS-2 — `polyrob goals` carries the same owner verbs as the chat seats:
pause writes `paused`, retry is the one re-queue, stop is once or --forever,
and `ready` / `resume` / `cancel` are gone."""
from click.testing import CliRunner

from agents.task.goals.board import GoalBoard
from cli.commands import goals as G


def _setup(tmp_path, monkeypatch):
    board = GoalBoard(str(tmp_path / "goals.db"))
    monkeypatch.setattr(G, "_get_board", lambda data_root=None: board)
    monkeypatch.setattr(G, "_owner_tenant", lambda user=None: "rob")
    return board


def test_deleted_verbs_are_gone():
    assert {"ready", "resume", "cancel"}.isdisjoint(G.goals.commands)
    assert {"pause", "retry", "stop"} <= set(G.goals.commands)


def test_pause_then_retry(tmp_path, monkeypatch):
    board = _setup(tmp_path, monkeypatch)
    g = board.create(user_id="rob", title="a goal")
    r = CliRunner().invoke(G.goals, ["pause", g.id])
    assert r.exit_code == 0, r.output
    assert board.get(g.id).status == "paused"
    r = CliRunner().invoke(G.goals, ["retry", g.id])
    assert r.exit_code == 0 and board.get(g.id).status == "ready"


def test_stop_once_and_forever(tmp_path, monkeypatch):
    from core.goal_suppressions import blocking
    board = _setup(tmp_path, monkeypatch)
    g = board.create(user_id="rob", title="gas watch")
    r = CliRunner().invoke(G.goals, ["stop", g.id])
    assert r.exit_code == 0 and board.get(g.id).status == "cancelled"
    assert not blocking(board.db_path, user_id="rob", title="gas watch")
    g2 = board.create(user_id="rob", title="gas watch", force=True)
    r = CliRunner().invoke(G.goals, ["stop", g2.id, "--forever"])
    assert r.exit_code == 0 and "switched OFF" in r.output
    assert blocking(board.db_path, user_id="rob", title="gas watch")


def test_another_tenants_goal_is_not_found(tmp_path, monkeypatch):
    board = _setup(tmp_path, monkeypatch)
    g = board.create(user_id="someone", title="theirs")
    r = CliRunner().invoke(G.goals, ["stop", g.id])
    assert r.exit_code == 1 and board.get(g.id).status == "ready"
