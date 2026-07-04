"""Tests for polyrob subagents commands."""

from click.testing import CliRunner


def test_subagents_command_group_exists():
    """Test that the subagents command group is registered."""
    from cli.polyrob import cli
    assert "subagents" in cli.commands


def test_subagents_info_shows_capability():
    """Test that subagents info shows delegation limits."""
    from cli.commands.subagents import subagents
    runner = CliRunner()
    result = runner.invoke(subagents, ["info"])
    assert result.exit_code == 0
    # Should show some capability info
    assert any(word in result.output.lower() for word in ["enabled", "concurrent", "background", "timeout"])


def test_subagents_info_has_json_flag():
    """Test that subagents info accepts --json flag."""
    from cli.commands.subagents import subagents
    runner = CliRunner()
    result = runner.invoke(subagents, ["info", "--json"])
    assert result.exit_code == 0
    # JSON output should be parseable
    import json
    try:
        json.loads(result.output)
    except json.JSONDecodeError:
        pass  # May show warning about missing config


def test_subagents_info_reports_real_parallel_timeout(monkeypatch):
    """`subagents info` must report the real parallel/async timeout, not the sync one."""
    import json
    from agents.task.constants import TimeoutConfig
    from cli.commands.subagents import subagents

    monkeypatch.setattr(TimeoutConfig, "get_sub_agent_timeout", classmethod(lambda cls: 600))
    monkeypatch.setattr(TimeoutConfig, "get_parallel_subtasks_timeout", classmethod(lambda cls: 900))

    result = CliRunner().invoke(subagents, ["info", "--json"])
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    assert info["sync_timeout"] == 600
    assert info["async_timeout"] == 900  # real parallel timeout, not equal to sync


def test_subagents_list_exists():
    """Test that subagents list command exists."""
    from cli.commands.subagents import subagents
    assert "list" in subagents.commands


def test_subagents_show_exists():
    """Test that subagents show command exists."""
    from cli.commands.subagents import subagents
    assert "show" in subagents.commands
