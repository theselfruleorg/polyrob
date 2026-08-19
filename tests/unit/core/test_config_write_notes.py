"""026 P0.6/P1.5/P1.6 — post-write honesty notes on every flag writer.

- P0.6: a global write silently shadowed by the project file (project beats
  global), and a process-env value that differs from the effective file value,
  now say so at write time (`unset` had the cross-scope hint; `set` had none).
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


def test_global_write_warns_when_project_shadows():
    _write_project("GOALS_ENABLED", "false")
    notes = post_write_notes("GOALS_ENABLED", "true", "global")
    assert any("shadowed by" in n and "config unset GOALS_ENABLED" in n
               for n in notes)


def test_no_shadow_note_when_values_agree():
    _write_project("GOALS_ENABLED", "true")
    notes = post_write_notes("GOALS_ENABLED", "true", "global")
    assert not any("shadowed" in n for n in notes)


def test_process_env_divergence_notes(monkeypatch):
    _write_project("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOALS_ENABLED", "false")
    notes = post_write_notes("GOALS_ENABLED", "true", "project")
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


def test_set_value_carries_notes_through():
    _write_project("GOALS_ENABLED", "false")
    result = set_value("GOALS_ENABLED", "true", scope="global")
    assert result.ok is True
    assert "shadowed by" in result.message


def test_cli_config_set_echoes_shadow_note(monkeypatch):
    import click.testing
    from cli.commands.config import config
    _write_project("GOALS_ENABLED", "false")
    runner = click.testing.CliRunner()
    result = runner.invoke(config, ["set", "GOALS_ENABLED", "true", "--global"])
    assert result.exit_code == 0, result.output
    assert "shadowed by" in result.output


def test_repl_config_set_appends_clamp_note(tmp_path):
    from cli.ui.commands.h_config import ConfigCtx, cmd_config
    out = cmd_config(ConfigCtx(user_id="local", home_dir=str(tmp_path)),
                     ["set", "AUTONOMY_MODE", "autonomous"])
    assert "Set AUTONOMY_MODE=autonomous" in out
    assert "CLAMP" in out
