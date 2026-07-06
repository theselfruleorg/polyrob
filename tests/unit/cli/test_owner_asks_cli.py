"""`polyrob owner asks/fulfill` — the owner surface for first-class asks (§7.2b)."""
from click.testing import CliRunner

from agents.task.goals.board import ASK_FULFILLED, GoalBoard, STATUS_READY


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "gleb")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")


def _board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_asks_empty(tmp_path, monkeypatch):
    from cli.commands.owner import owner
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["asks"])
    assert res.exit_code == 0
    assert "no open asks" in res.output


def test_asks_lists_open(tmp_path, monkeypatch):
    from cli.commands.owner import owner
    _env(tmp_path, monkeypatch)
    _board(tmp_path).create_ask(user_id="gleb", what="Grant Twitter write access",
                                why="X objective needs twitter_post")
    res = CliRunner().invoke(owner, ["asks"])
    assert res.exit_code == 0
    assert "Grant Twitter write access" in res.output
    assert "fulfill" in res.output  # tells the owner how to act


def test_fulfill_unblocks(tmp_path, monkeypatch):
    from cli.commands.owner import owner
    _env(tmp_path, monkeypatch)
    board = _board(tmp_path)
    g = board.create(user_id="gleb", title="Post the launch thread on X")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="no write access")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="no write access")  # blocked
    a = board.create_ask(user_id="gleb", what="Grant Twitter write access",
                         blocks_goal_ids=[g.id])
    res = CliRunner().invoke(owner, ["fulfill", a.id])
    assert res.exit_code == 0
    assert board.get(a.id).status == ASK_FULFILLED
    assert board.get(g.id).status == STATUS_READY


def test_fulfill_unknown_ask_exits_nonzero(tmp_path, monkeypatch):
    from cli.commands.owner import owner
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["fulfill", "nope"])
    assert res.exit_code != 0
