from pathlib import Path
from click.testing import CliRunner

from core.runtime_paths import _local_default_data_home as _REAL_LOCAL_DATA_HOME


def test_init_writes_files(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / ".git").mkdir()  # 027 WP5: init gitignores only inside a git work tree
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    # the REAL local default (conftest points it at the cwd for isolation)
    import core.runtime_paths as _rp
    monkeypatch.setattr(_rp, "_local_default_data_home", _REAL_LOCAL_DATA_HOME)
    from cli.commands.init import init_cmd
    res = CliRunner().invoke(init_cmd, ["--anthropic-key", "sk-x", "--default-model", "claude-opus-4-8", "--no-prompt"])
    assert res.exit_code == 0
    env = (home / ".polyrob" / ".env").read_text()
    assert "ANTHROPIC_API_KEY=sk-x" in env and "DEFAULT_MODEL=claude-opus-4-8" in env
    assert (home / ".polyrob" / ".env").stat().st_mode & 0o777 == 0o600
    assert (home / ".polyrob" / "data" / "sessions").is_dir()
    assert not (proj / ".polyrob" / "sessions").exists()  # cwd/.polyrob is never loaded
    assert ".polyrob/" in (proj / ".gitignore").read_text()
