"""P3 — CLI defaults store + new model/skills commands."""
from click.testing import CliRunner

from cli.config_store import (
    load_cli_config, save_cli_config, set_default_model, get_default_model,
)


def test_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(tmp_path / "cli.json"))
    assert load_cli_config() == {}
    assert get_default_model() == (None, None)


def test_set_and_get_default_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(tmp_path / "nested" / "cli.json"))
    # set_default_model (G11 SSOT fix) also upserts ~/.polyrob/.env — keep it off
    # the real home dir so this test never mutates the developer's actual config.
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    set_default_model("anthropic", "claude-opus-4-8")
    assert get_default_model() == ("anthropic", "claude-opus-4-8")
    # persisted to disk
    assert load_cli_config()["default_model"] == "claude-opus-4-8"


def test_corrupt_file_returns_empty(tmp_path, monkeypatch):
    p = tmp_path / "cli.json"
    p.write_text("{not valid json")
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(p))
    assert load_cli_config() == {}


def test_save_preserves_other_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(tmp_path / "cli.json"))
    # See test_set_and_get_default_round_trip above — keep the env-pin write off
    # the real home dir.
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    save_cli_config({"foo": "bar"})
    set_default_model("openai", "gpt-5")
    cfg = load_cli_config()
    assert cfg["foo"] == "bar" and cfg["default_provider"] == "openai"


# --- CLI commands ------------------------------------------------------------

def test_model_set_default_command(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(tmp_path / "cli.json"))
    # See test_set_and_get_default_round_trip above — keep the env-pin write off
    # the real home dir.
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["model", "set-default", "anthropic", "claude-opus-4-8"])
    assert result.exit_code == 0, result.output
    assert get_default_model() == ("anthropic", "claude-opus-4-8")


def test_skills_group_help():
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["skills", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "validate" in result.output


def test_skills_list_uses_manager(monkeypatch):
    import cli.polyrob as robcli

    class _FakeMgr:
        skill_rules = {"research": {}, "coder": {}}

        def _ensure_rules_loaded(self):
            pass

    monkeypatch.setattr("cli.commands.skills.get_skill_manager", lambda: _FakeMgr())
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["skills", "list"])
    assert result.exit_code == 0, result.output
    assert "research" in result.output and "coder" in result.output
