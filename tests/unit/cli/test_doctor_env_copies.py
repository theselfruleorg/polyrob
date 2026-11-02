"""``polyrob doctor`` names an operator's env backup that holds a wallet key."""
import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".polyrob"
    h.mkdir()
    monkeypatch.setenv("POLYROB_HOME", str(h))
    monkeypatch.setattr("core.runtime_paths.effective_data_home", lambda: h / "data")
    return h


def test_a_backup_with_the_seed_is_named(home, monkeypatch):
    from cli.commands import doctor_env_copies as mod
    (home / ".env").write_text("X=1\n")
    (home / ".env.bak-20261028").write_text("X=1\n")
    (home / ".env.orig").write_text("X=1\n")
    monkeypatch.setattr("core.security.custody_env.file_holds_custody_secret",
                        lambda p: p.name == ".env.bak-20261028" or p.name == ".env")
    lines = mod.env_copy_lines({})
    assert len(lines) == 1
    assert ".env.bak-20261028" in lines[0] and ".env.orig" not in lines[0]
    assert str(home / ".env,") not in lines[0]


def test_no_copy_no_line(home, monkeypatch):
    from cli.commands import doctor_env_copies as mod
    (home / ".env").write_text("X=1\n")
    monkeypatch.setattr("core.security.custody_env.file_holds_custody_secret", lambda p: True)
    assert mod.env_copy_lines({}) == []


def test_registered_with_the_doctor():
    from cli.commands import doctor_host
    names = {c.__name__ for c in doctor_host._checkers()}
    assert "env_copy_lines" in names
