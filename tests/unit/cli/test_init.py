from pathlib import Path
from click.testing import CliRunner


def test_init_writes_files(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    proj = tmp_path / "proj"; proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    from cli.commands.init import init_cmd
    res = CliRunner().invoke(init_cmd, ["--anthropic-key", "sk-x", "--default-model", "claude-opus-4-8", "--no-prompt"])
    assert res.exit_code == 0
    env = (home / ".polyrob" / ".env").read_text()
    assert "ANTHROPIC_API_KEY=sk-x" in env and "DEFAULT_MODEL=claude-opus-4-8" in env
    assert (home / ".polyrob" / ".env").stat().st_mode & 0o777 == 0o600
    assert (proj / ".polyrob" / "sessions").is_dir()
    assert ".polyrob/" in (proj / ".gitignore").read_text()
