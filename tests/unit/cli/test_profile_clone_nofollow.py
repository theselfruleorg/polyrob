"""Audit 2026-11-21 H-3 + M-8: a root profile clone never follows a link, and
the hand-over walk is descriptor-relative and hands the top over LAST."""
import os
import stat
import types

import pytest
from click.testing import CliRunner

from cli.commands import profile as prof
from cli.commands.profile import profile


@pytest.fixture
def env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("POLYROB_HOME", str(home))
    monkeypatch.setenv("POLYROB_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    monkeypatch.delenv("POLYROB_PROFILES_ROOT", raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return home


def _run(*args):
    return CliRunner().invoke(profile, list(args), catch_exceptions=False)


def _secret(tmp_path):
    s = tmp_path / "wallet.env"
    s.write_text("SEED=do-not-copy\n")
    return s


def test_clone_refuses_a_linked_config_file(env, tmp_path):
    _run("create", "src", "--no-alias")
    src = env / "profiles" / "src"
    (src / "auth.json").symlink_to(_secret(tmp_path))
    r = _run("create", "dst", "--from", "src", "--no-alias")
    assert r.exit_code != 0
    assert "link" in r.output
    assert not (env / "profiles" / "dst").exists()   # no half clone left


def test_clone_refuses_a_link_inside_a_cloned_dir(env, tmp_path):
    _run("create", "src", "--no-alias")
    src = env / "profiles" / "src"
    (src / "characters" / "ok.character.json").write_text("{}")
    (src / "characters" / "evil.character.json").symlink_to(_secret(tmp_path))
    r = _run("create", "dst", "--from", "src", "--no-alias")
    assert r.exit_code != 0
    assert not (env / "profiles" / "dst").exists()


def test_clone_refuses_a_linked_cloned_dir(env, tmp_path):
    _run("create", "src", "--no-alias")
    src = env / "profiles" / "src"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "id_rsa").write_text("KEY")
    (src / "skills").rmdir()
    (src / "skills").symlink_to(outside)
    r = _run("create", "dst", "--from", "src", "--no-alias")
    assert r.exit_code != 0
    assert not (env / "profiles" / "dst").exists()


def test_clone_copies_a_real_tree_with_its_modes(env):
    _run("create", "src", "--no-alias")
    src = env / "profiles" / "src"
    (src / "skills" / "a" / "b").mkdir(parents=True)
    (src / "skills" / "a" / "b" / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(src / "skills" / "a" / "b" / "run.sh", 0o750)
    (src / "auth.json").write_text("{}")
    os.chmod(src / "auth.json", 0o600)
    r = _run("create", "dst", "--from", "src", "--no-alias")
    assert r.exit_code == 0, r.output
    dst = env / "profiles" / "dst"
    assert (dst / "skills" / "a" / "b" / "run.sh").read_text() == "#!/bin/sh\n"
    assert stat.S_IMODE(os.stat(dst / "skills" / "a" / "b" / "run.sh").st_mode) == 0o750
    assert stat.S_IMODE(os.stat(dst / "auth.json").st_mode) == 0o600


def test_handover_is_bottom_up_and_hands_the_top_last(tmp_path, monkeypatch):
    home = tmp_path / "p"
    (home / "data" / "identity").mkdir(parents=True)
    (home / "data" / "identity" / "identity.md").write_text("x")
    (home / ".env").write_text("A=1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("x")
    (home / "data" / "linked").symlink_to(outside)
    order = []
    monkeypatch.setattr(prof.os, "geteuid", lambda: 0)
    monkeypatch.setattr(prof.os, "fchown",
                        lambda fd, u, g: order.append(os.fstat(fd).st_ino))
    import grp
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda n: types.SimpleNamespace(pw_uid=4242))
    monkeypatch.setattr(grp, "getgrnam", lambda n: types.SimpleNamespace(gr_gid=4343))
    real_fchmod = os.fchmod
    monkeypatch.setattr(prof.os, "fchmod", lambda fd, m: real_fchmod(fd, m & 0o777))
    prof._hand_to_service_user(home)
    ino = lambda p: os.lstat(p).st_ino  # noqa: E731
    assert order[-1] == ino(home)                                  # the top goes LAST
    assert order.index(ino(home / "data" / "identity" / "identity.md")) \
        < order.index(ino(home / "data" / "identity")) < order.index(ino(home / "data"))
    assert ino(outside) not in order and ino(outside / "f") not in order
    assert stat.S_IMODE(os.stat(outside).st_mode) != 0o770


# --- --from-project takes the same no-follow clone -------------------------------

def _legacy(tmp_path):
    d = tmp_path / "legacy" / ".polyrob"
    (d / "identity").mkdir(parents=True)
    (d / "identity" / "identity.md").write_text("ID\n")
    (d / "characters").mkdir()
    (d / "characters" / "nova.character.json").write_text("{}")
    (d / "sessions" / "s1").mkdir(parents=True)
    (d / "sessions" / "s1" / "log.txt").write_text("L")
    return d


def _refused(env, tmp_path, **kw):
    from cli.commands.profile import create_profile
    import click
    with pytest.raises(click.ClickException, match="link"):
        create_profile("dst", from_project=str(tmp_path / "legacy"), **kw)
    assert not (env / "profiles" / "dst").exists()


def test_from_project_refuses_a_link_in_identity(env, tmp_path):
    d = _legacy(tmp_path)
    (d / "identity" / "operating.md").symlink_to(_secret(tmp_path))
    _refused(env, tmp_path)


def test_from_project_refuses_a_linked_characters_dir(env, tmp_path):
    d = _legacy(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.character.json").write_text("{}")
    import shutil
    shutil.rmtree(d / "characters")
    (d / "characters").symlink_to(outside)
    _refused(env, tmp_path)


def test_from_project_refuses_a_linked_env(env, tmp_path):
    d = _legacy(tmp_path)
    (d / ".env").symlink_to(_secret(tmp_path))
    _refused(env, tmp_path)


def test_from_project_refuses_a_linked_or_hard_linked_db_and_sessions(env, tmp_path):
    d = _legacy(tmp_path)
    (d / "memory.db").symlink_to(_secret(tmp_path))
    _refused(env, tmp_path, include_data=True)
    (d / "memory.db").unlink()
    os.link(_secret(tmp_path), d / "memory.db")
    _refused(env, tmp_path, include_data=True)
    (d / "memory.db").unlink()
    (d / "sessions" / "s1" / "leak").symlink_to(_secret(tmp_path))
    _refused(env, tmp_path, include_data=True)


def test_from_project_copies_a_real_tree(env, tmp_path):
    from cli.commands.profile import create_profile
    import sqlite3
    d = _legacy(tmp_path)
    con = sqlite3.connect(d / "memory.db")
    con.execute("create table t(x)")
    con.commit()
    con.close()
    r = create_profile("dst", from_project=str(tmp_path / "legacy"), include_data=True)
    home = r["home"]
    assert (home / "data" / "identity" / "identity.md").read_text() == "ID\n"
    assert (home / "characters" / "nova.character.json").is_file()
    assert (home / "data" / "sessions" / "s1" / "log.txt").read_text() == "L"
    assert (home / "data" / "memory.db").is_file()


def test_a_missing_clone_source_makes_nothing(env):
    from core.profiles import ProfileNotFoundError
    with pytest.raises(ProfileNotFoundError):
        prof.create_profile("dst", from_profile="nosuch")
    assert not (env / "profiles" / "dst").exists()   # a retry is not "already exists"


def test_a_path_reader_goes_through_the_pinned_dir(tmp_path):
    """A swapped parent is not followed: on Linux the reader's path is the opened
    directory itself (/proc/self/fd/<n>); without procfs it is the checked path."""
    src = tmp_path / "proj" / ".polyrob"
    src.mkdir(parents=True)
    (src / ".env").write_text("POLYROB_INSTANCE_ID=real\n")
    fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY)
    try:
        pinned = prof._pinned_dir(fd, src)
        if not os.path.isdir("/proc/self/fd"):
            assert pinned == src
            return
        assert str(pinned).startswith("/proc/self/fd/")
        os.rename(src, tmp_path / "proj" / "moved")       # the parent is swapped
        decoy = tmp_path / "proj" / ".polyrob"
        decoy.mkdir()
        (decoy / ".env").write_text("POLYROB_INSTANCE_ID=decoy\n")
        assert "real" in (pinned / ".env").read_text()
    finally:
        os.close(fd)
