"""SSOT fix (G11, 2026-07-14): `/model … set-default` must keep the DEFAULT_*
env pin (~/.polyrob/.env, precedence 2) in lockstep with cli.json (precedence
3) — otherwise a `polyrob init`-written env pin silently overrides a later
`/model set-default` for every future session. See cli/config_store.py.
"""
from cli import config_store


def test_set_default_model_updates_env_pin(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DEFAULT_PROVIDER=openrouter\nDEFAULT_MODEL=old/model\n")
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(tmp_path / "cli.json"))
    config_store.set_default_model("anthropic", "claude-sonnet-5")
    text = env_file.read_text()
    assert "DEFAULT_PROVIDER=anthropic" in text
    assert "DEFAULT_MODEL=claude-sonnet-5" in text
    # cli.json still written (aliases/back-compat readers)
    assert config_store.get_default_model() == ("anthropic", "claude-sonnet-5")


def test_set_default_model_creates_env_pin_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(tmp_path / "cli.json"))
    config_store.set_default_model("openai", "gpt-5")
    text = (tmp_path / ".env").read_text()
    assert "DEFAULT_PROVIDER=openai" in text and "DEFAULT_MODEL=gpt-5" in text


def test_set_default_model_env_write_failure_is_nonfatal(monkeypatch, tmp_path):
    monkeypatch.setattr("core.paths.polyrob_home", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(tmp_path / "cli.json"))
    config_store.set_default_model("openai", "gpt-5")   # must not raise
