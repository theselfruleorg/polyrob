"""026 P0.6/P1.5/P1.6 — post-write honesty notes on every flag writer.

- P0.6: a write into the project file (never loaded since the 2026-10-07
  security pass), and a process-env value that differs from the effective file
  value, say so at write time.
- P1.6: writing AUTONOMY_MODE=autonomous evaluates the single-owner clamp NOW
  and names the missing prerequisite (the runtime's one-time WARN is invisible
  at the console's default ERROR level).
- P1.5: a remote surface (webview) is told when the serving process reads the
  server env ladder, so the .polyrob write applies to CLI runs only.
"""
import pytest

from core.config_service import post_write_notes, set_value


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    for var in ("GOALS_ENABLED", "AUTONOMY_MODE", "POLYROB_LOCAL", "ROB_LOCAL",
                "POLYROB_OWNER_USER_ID", "POLYROB_OWNER_EMAIL",
                "POLYROB_OWNER_TELEGRAM_ID"):
        monkeypatch.delenv(var, raising=False)
    from core.config_policy import policy
    policy.reset_autonomy_mode_warnings()
    yield
    policy.reset_autonomy_mode_warnings()


def _write_project(key, value, tmp_path=None):
    import pathlib
    dotdir = pathlib.Path.cwd() / ".polyrob"
    dotdir.mkdir(exist_ok=True)
    with open(dotdir / ".env", "a") as f:
        f.write(f"{key}={value}\n")


def test_project_file_never_shadows_a_global_write():
    """Regression: ./.polyrob/.env is not loaded (core.paths.env_file_candidates),
    so a global write must not be told it 'has no effect'."""
    _write_project("GOALS_ENABLED", "false")
    notes = post_write_notes("GOALS_ENABLED", "true", "global")
    assert not any("shadowed" in n or "no effect" in n for n in notes)


def test_project_write_is_told_the_file_is_not_loaded():
    notes = post_write_notes("GOALS_ENABLED", "true", "project")
    assert any("is not loaded" in n and "--global" in n for n in notes)


def _write_global(key, value):
    import os, pathlib
    home = pathlib.Path(os.environ["POLYROB_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    with open(home / ".env", "a") as f:
        f.write(f"{key}={value}\n")


def test_process_env_divergence_notes(monkeypatch):
    _write_global("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOALS_ENABLED", "false")
    notes = post_write_notes("GOALS_ENABLED", "true", "global")
    assert any("this process started with GOALS_ENABLED=false" in n
               for n in notes)


def test_clamp_echo_names_missing_prerequisite():
    notes = post_write_notes("AUTONOMY_MODE", "autonomous", "project")
    clamp = next(n for n in notes if "CLAMP" in n)
    assert "POLYROB_LOCAL" in clamp


def test_clamp_echo_effective_when_owner_bound(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-uid")
    notes = post_write_notes("AUTONOMY_MODE", "autonomous", "project")
    assert any("autonomous (effective)" in n for n in notes)
    assert not any("CLAMP" in n for n in notes)


def test_remote_surface_server_ladder_note():
    notes = post_write_notes("GOALS_ENABLED", "true", "project", surface="console")
    assert any("CLI runs only" in n for n in notes)


def test_local_surface_has_no_server_note(monkeypatch):
    notes = post_write_notes("GOALS_ENABLED", "true", "project", surface="local")
    assert not any("CLI runs only" in n for n in notes)


def test_set_value_refuses_a_project_write():
    """A write into a file that is never loaded is refused, not written."""
    import pathlib
    result = set_value("GOALS_ENABLED", "true", scope="project")
    assert result.ok is False and result.outcome == "refused"
    assert "does not load the project env file" in result.message
    assert not (pathlib.Path.cwd() / ".polyrob" / ".env").exists()


def test_cli_config_set_project_is_refused_with_the_read_file(tmp_path):
    import click.testing
    from cli.commands.config import config
    runner = click.testing.CliRunner()
    result = runner.invoke(config, ["set", "GOALS_ENABLED", "true", "--project"])
    assert result.exit_code != 0
    assert "does not load the project env file" in result.output
    assert str(tmp_path / "home" / ".env") in result.output
    assert not (tmp_path / ".polyrob" / ".env").exists()


def test_repl_config_set_appends_clamp_note(tmp_path):
    from cli.ui.commands.h_config import ConfigCtx, cmd_config
    out = cmd_config(ConfigCtx(user_id="local", home_dir=str(tmp_path)),
                     ["set", "AUTONOMY_MODE", "autonomous"])
    assert "Set AUTONOMY_MODE=autonomous" in out
    assert "CLAMP" in out
