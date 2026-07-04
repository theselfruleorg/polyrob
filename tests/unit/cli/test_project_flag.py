"""`rob --project <path>` sets POLYROB_PROJECT_DIR before any subcommand builds a
container — the launch-in-a-folder ergonomic (Claude-Code style)."""

import os

from click.testing import CliRunner


def test_project_flag_sets_env(monkeypatch, tmp_path):
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    from cli.polyrob import cli
    runner = CliRunner()
    # `version` is a cheap subcommand that exercises the group callback without
    # building a container or starting the REPL.
    result = runner.invoke(cli, ["--project", str(tmp_path), "version"])
    assert result.exit_code == 0
    assert os.environ["POLYROB_PROJECT_DIR"] == str(tmp_path.resolve())


def test_no_project_flag_leaves_env_unset(monkeypatch):
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 0
    assert "POLYROB_PROJECT_DIR" not in os.environ
