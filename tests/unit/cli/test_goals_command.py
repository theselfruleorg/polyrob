"""Tests for polyrob goals commands."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


def test_goals_command_group_exists():
    """Test that the goals command group is registered."""
    from cli.polyrob import cli
    assert "goals" in cli.commands


def test_goals_list_requires_data_root():
    """Test that goals list requires a valid data root."""
    from cli.polyrob import cli
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        result = runner.invoke(
            cli,
            ["goals", "list"],
            env={"POLYROB_DATA_DIR": tmpdir},
        )
        # Should not crash - may show "No goals found" or similar
        assert result.exit_code == 0 or "No goals found" in result.output


def test_goals_info_shows_capability():
    """Test that subagents info shows delegation limits."""
    from cli.commands.subagents import subagents
    runner = CliRunner()
    result = runner.invoke(subagents, ["info"])
    assert result.exit_code == 0
    assert "delegation" in result.output.lower() or "enabled" in result.output.lower()


def test_goals_list_json_acceptance():
    """Test that goals list accepts --json flag."""
    from cli.commands.goals import goals
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        result = runner.invoke(
            goals,
            ["list", "--json"],
            env={"POLYROB_DATA_DIR": tmpdir},
        )
        # Should not crash on --json flag
        assert result.exit_code == 0


def test_goals_events_renders_without_double_decode(tmp_path):
    """`goals events` must not re-json.loads an already-parsed payload dict."""
    from cli.commands import goals as G
    from agents.task.goals.board import GoalBoard

    db = tmp_path / "goals.db"
    board = GoalBoard(str(db))
    g = board.create(user_id="u1", title="demo goal")  # emits a 'created' event with payload

    runner = CliRunner()
    with patch.object(G, "_get_board", lambda *a, **k: board):
        # human-readable path (the one that crashed with TypeError on a dict)
        result = runner.invoke(G.goals, ["events", g.id])
        assert result.exit_code == 0, result.output
        assert "created" in result.output
        # JSON path stays valid too
        result_json = runner.invoke(G.goals, ["events", g.id, "--json"])
        assert result_json.exit_code == 0, result_json.output
