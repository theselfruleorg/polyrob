from pathlib import Path
from click.testing import CliRunner


def test_config_set_global_and_show(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    from cli.commands.config import config as config_group
    r = CliRunner()
    res = r.invoke(config_group, ["set", "ANTHROPIC_API_KEY", "sk-secret", "--global"])
    assert res.exit_code == 0, res.output
    assert "ANTHROPIC_API_KEY=sk-secret" in (home / ".polyrob" / ".env").read_text()
    assert (home / ".polyrob" / ".env").stat().st_mode & 0o777 == 0o600
    res = r.invoke(config_group, ["set", "DEFAULT_MODEL", "claude-opus-4-8"])
    assert res.exit_code == 0, res.output
    assert "DEFAULT_MODEL=claude-opus-4-8" in (proj / ".polyrob" / ".env").read_text()
    res = r.invoke(config_group, ["show"])
    assert res.exit_code == 0, res.output
    assert "sk-secret" not in res.output            # secret redacted
    assert "ANTHROPIC_API_KEY" in res.output        # key name shown
    assert "claude-opus-4-8" in res.output          # non-secret shown


def test_config_set_project_gitignores_polyrob(tmp_path, monkeypatch):
    # A project-scope `config set` writes a secret to ./.polyrob/.env; it must add
    # .polyrob/ to .gitignore so a subsequent `git add` can't leak it (the gap:
    # only init/run gitignored it before, neither of which has run yet).
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()  # a git repo → require_git_repo=True should write
    monkeypatch.chdir(proj)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["set", "OPENAI_API_KEY", "sk-realkey123456"])
    assert res.exit_code == 0, res.output
    gi = proj / ".gitignore"
    assert gi.exists(), "config set (project scope) must create/append .gitignore"
    assert any(ln.strip() == ".polyrob/" for ln in gi.read_text().splitlines())


def test_config_set_no_duplicate_on_spaced_key(tmp_path, monkeypatch):
    # A hand-edited line `KEY = old` (spaces around `=`) was indexed under
    # "KEY " (trailing space), so `config set KEY new` failed to find it and
    # appended a duplicate `KEY=new`. It must instead update the existing line.
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    env = home / ".polyrob" / ".env"
    env.write_text("DEFAULT_MODEL = old\n")
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["set", "DEFAULT_MODEL", "new", "--global"])
    assert res.exit_code == 0, res.output
    lines = [ln for ln in env.read_text().splitlines() if ln.strip()]
    key_lines = [ln for ln in lines if ln.split("=", 1)[0].strip() == "DEFAULT_MODEL"]
    assert key_lines == ["DEFAULT_MODEL=new"], f"expected one updated line, got {lines}"


def test_config_show_redacts_jwt(tmp_path, monkeypatch):
    # ANYSITE_JWT is a real credential (bootstrap secret allowlist) but the old
    # _SECRET_HINTS missed 'JWT' so `config show` printed it in cleartext.
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    (home / ".polyrob" / ".env").write_text("ANYSITE_JWT=eyJhbGcabcdefghij123\n")
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["show"])
    assert res.exit_code == 0, res.output
    assert "eyJhbGcabcdefghij123" not in res.output  # value redacted
    assert "ANYSITE_JWT" in res.output               # key name still shown


def test_config_path_lists_files(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; (proj / ".polyrob").mkdir(parents=True)
    (home / ".polyrob" / ".env").write_text("A=1\n")
    (proj / ".polyrob" / ".env").write_text("B=2\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["path"])
    assert res.exit_code == 0, res.output
    assert str(home / ".polyrob" / ".env") in res.output
    assert str(proj / ".polyrob" / ".env") in res.output
