"""Local dotenv layering + cli.json migration (R3)."""
import os
from pathlib import Path
import pytest


def test_project_env_overrides_home(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; (proj / ".polyrob").mkdir(parents=True)
    (home / ".polyrob" / ".env").write_text("DEFAULT_MODEL=global-model\n")
    (proj / ".polyrob" / ".env").write_text("DEFAULT_MODEL=project-model\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    monkeypatch.delenv("DEFAULT_MODEL", raising=False)
    from core.bootstrap import load_env
    load_env(local_mode=True)
    assert os.environ["DEFAULT_MODEL"] == "project-model"


def test_process_env_wins_over_rob_files(tmp_path, monkeypatch):
    """An explicitly-exported process env var must NOT be clobbered by .polyrob/.env."""
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; (proj / ".polyrob").mkdir(parents=True)
    (home / ".polyrob" / ".env").write_text("DEFAULT_MODEL=home-model\n")
    (proj / ".polyrob" / ".env").write_text("DEFAULT_MODEL=project-model\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    monkeypatch.setenv("DEFAULT_MODEL", "shell-model")  # exported before launch
    from core.bootstrap import load_env
    load_env(local_mode=True)
    assert os.environ["DEFAULT_MODEL"] == "shell-model"  # process env wins


def test_migrate_cli_json_to_dotenv(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(home / ".polyrob" / "cli.json"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    import json
    (home / ".polyrob" / "cli.json").write_text(json.dumps(
        {"default_provider": "anthropic", "default_model": "claude-opus-4-8"}))
    from cli.config_store import migrate_to_dotenv
    migrate_to_dotenv()
    env = (home / ".polyrob" / ".env").read_text()
    assert "DEFAULT_PROVIDER=anthropic" in env
    assert "DEFAULT_MODEL=claude-opus-4-8" in env
    assert not (home / ".polyrob" / "cli.json").exists()  # migrated then removed
