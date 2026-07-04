"""Tests for the rob CLI entry point."""

import pytest
from click.testing import CliRunner


def test_version_command():
    """rob version prints version info without spinning up uvicorn."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 0
    assert "rob" in result.output.lower()


def test_cli_help():
    """rob --help shows available commands."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.output
    assert "chat" in result.output


# --- PR4: Full CLI commands ---


def test_run_help():
    """polyrob run --help shows model/provider/tools options."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "--model" in result.output
    assert "--provider" in result.output
    assert "--tools" in result.output
    assert "--max-steps" in result.output
    assert "--verbose" in result.output
    assert "--plain" in result.output


def test_chat_help():
    """polyrob chat exists as an explicit REPL command."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["chat", "--help"])
    assert result.exit_code == 0
    assert "--plain" in result.output


def test_session_subgroup():
    """rob session --help shows list/tail/cancel subcommands."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["session", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "tail" in result.output
    assert "cancel" in result.output


def test_sessions_alias():
    """polyrob sessions is a product-vocabulary alias for session."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["sessions", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "tail" in result.output


def test_model_list_help():
    """rob model list --help exists."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["model", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output


def test_models_alias():
    """polyrob models is a product-vocabulary alias for model."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["models", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output


def test_tools_catalog_json():
    """polyrob tools list --json exposes a machine-readable catalog."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["tools", "list", "--json"])
    assert result.exit_code == 0
    assert '"id": "filesystem"' in result.output
    assert '"permissions"' in result.output


def test_model_list_shows_providers():
    """rob model list displays provider/status table."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["model", "list"])
    assert result.exit_code == 0
    assert "Provider" in result.output
    assert "openai" in result.output
    assert "anthropic" in result.output
    assert "gemini" in result.output


def test_run_requires_task_argument():
    """polyrob run without a task argument shows usage error."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["run"])
    assert result.exit_code != 0
    assert "Missing argument" in result.output or "TASK" in result.output


def test_session_tail_requires_id():
    """rob session tail without ID shows usage error."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["session", "tail"])
    assert result.exit_code != 0


def test_session_cancel_requires_id():
    """rob session cancel without ID shows usage error."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["session", "cancel"])
    assert result.exit_code != 0
