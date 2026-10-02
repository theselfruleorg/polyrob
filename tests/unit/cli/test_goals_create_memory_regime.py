"""025: `polyrob goals create --memory-regime` — refused while scopes are off."""
from unittest.mock import patch

from click.testing import CliRunner


def _invoke(tmp_path, monkeypatch, enabled):
    from cli.commands import goals as G
    from agents.task.goals.board import GoalBoard
    monkeypatch.setenv("MEMORY_SCOPES_ENABLED", "true" if enabled else "false")
    monkeypatch.setattr(G, "_owner_tenant", lambda user=None: "u1")
    board = GoalBoard(str(tmp_path / "goals.db"))
    with patch.object(G, "_get_board", return_value=board):
        out = CliRunner().invoke(G.goals, ["create", "iso goal", "--memory-regime", "sealed"],
                                 env={"POLYROB_DATA_DIR": str(tmp_path)})
    return out, board


def test_refused_while_scopes_are_off(tmp_path, monkeypatch):
    out, board = _invoke(tmp_path, monkeypatch, enabled=False)
    assert out.exit_code != 0 and "MEMORY_SCOPES_ENABLED" in out.output
    assert board.list_recent(user_id="u1") == []


def test_stored_when_scopes_are_on(tmp_path, monkeypatch):
    out, board = _invoke(tmp_path, monkeypatch, enabled=True)
    assert out.exit_code == 0, out.output
    [g] = board.list_recent(user_id="u1")
    assert g.payload["memory_regime"] == "sealed"
