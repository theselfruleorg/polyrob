"""polyrob profile — command-group tests (multi-instance W3).

Isolated via POLYROB_HOME + POLYROB_BIN_DIR; nothing touches the real
~/.polyrob or ~/.local/bin.
"""
import json
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.commands.profile import create_profile, profile


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


def test_create_list_use_show_delete_roundtrip(env, tmp_path):
    r = _run("create", "scout", "--description", "test bot", "--no-alias")
    assert r.exit_code == 0, r.output
    p = env / "profiles" / "scout"
    assert (p / "profile.yaml").is_file()
    assert (p / "characters").is_dir() and (p / "skills").is_dir() and (p / "data").is_dir()
    assert "POLYROB_INSTANCE_ID=scout" in (p / ".env").read_text()

    r = _run("list")
    assert "scout" in r.output and "test bot" in r.output

    r = _run("use", "scout")
    assert r.exit_code == 0
    assert (env / "active_profile").read_text().strip() == "scout"
    assert "*" in _run("list").output  # active marker

    r = _run("show", "scout")
    assert "scout" in r.output and str(p) in r.output

    r = _run("path", "scout")
    assert r.output.strip() == str(p)

    r = _run("delete", "scout", "--yes")
    assert r.exit_code == 0
    assert not p.exists()
    assert not (env / "active_profile").exists()  # sticky cleared with it


def test_create_refuses_existing_and_bad_names(env):
    _run("create", "scout", "--no-alias")
    r = CliRunner().invoke(profile, ["create", "scout", "--no-alias"])
    assert r.exit_code != 0 and "already exists" in r.output
    r = CliRunner().invoke(profile, ["create", "../evil", "--no-alias"])
    assert r.exit_code != 0 and "invalid profile name" in r.output


def test_delete_refuses_live_profile(env):
    _run("create", "scout", "--no-alias")
    (env / "profiles" / "scout" / "data" / "memory.db-wal").write_text("")
    r = CliRunner().invoke(profile, ["delete", "scout", "--yes"])
    assert r.exit_code != 0 and "in use" in r.output
    assert (env / "profiles" / "scout").exists()
    r = CliRunner().invoke(profile, ["delete", "scout", "--yes", "--force"])
    assert r.exit_code == 0
    assert not (env / "profiles" / "scout").exists()


def test_from_project_copies_identity_not_dbs(env, tmp_path):
    proj = tmp_path / "legacy"
    d = proj / ".polyrob"
    (d / "identity" / "rob" / "user_owner").mkdir(parents=True)
    (d / "identity" / "rob" / "user_owner" / "self.md").write_text("SELF\n")
    (d / "characters").mkdir()
    (d / "characters" / "rob.character.json").write_text(json.dumps({"name": "Rob"}))
    (d / ".env").write_text("POLYROB_INSTANCE_ID=rob\nANTHROPIC_API_KEY=sk-x\n")
    (d / "memory.db").write_text("NOT-COPIED-BY-DEFAULT")

    result = create_profile("rob", from_project=str(proj))
    home = result["home"]
    assert (home / "data" / "identity" / "rob" / "user_owner" / "self.md").read_text() == "SELF\n"
    assert (home / "characters" / "rob.character.json").is_file()
    env_text = (home / ".env").read_text()
    assert "POLYROB_INSTANCE_ID=rob" in env_text  # identity key lifted, wins over default
    assert "ANTHROPIC_API_KEY" not in env_text  # provider keys are NOT identity
    assert not (home / "data" / "memory.db").exists()  # DBs only with --include-data
    # non-destructive: the legacy folder is byte-identical
    assert (d / "memory.db").read_text() == "NOT-COPIED-BY-DEFAULT"
    assert (d / "identity" / "rob" / "user_owner" / "self.md").exists()


def test_from_project_include_data_copies_dbs(env, tmp_path):
    proj = tmp_path / "legacy"
    d = proj / ".polyrob"
    d.mkdir(parents=True)
    (d / "memory.db").write_text("MEM")
    result = create_profile("rob2", from_project=str(proj), include_data=True)
    assert (result["home"] / "data" / "memory.db").read_text() == "MEM"


def test_clone_from_profile_gets_own_instance_id(env):
    _run("create", "rob", "--no-alias")
    p = env / "profiles" / "rob"
    (p / ".env").write_text("POLYROB_INSTANCE_ID=rob\nPERSONALITY_DEFAULT_CHARACTER=rob\n")
    (p / "characters" / "rob.character.json").write_text(json.dumps({"name": "Rob"}))
    r = _run("create", "rob2", "--from", "rob", "--no-alias")
    assert r.exit_code == 0, r.output
    p2 = env / "profiles" / "rob2"
    env_text = (p2 / ".env").read_text()
    assert "POLYROB_INSTANCE_ID=rob2" in env_text  # a clone is a NEW instance
    assert "PERSONALITY_DEFAULT_CHARACTER=rob" in env_text  # voice cloned
    assert (p2 / "characters" / "rob.character.json").is_file()


def _assert_wrapper_command(text, profile_name):
    command = next(line for line in text.splitlines() if line.startswith("exec "))
    words = shlex.split(command)
    assert words[0] == "exec"
    assert Path(words[1]).name in ("polyrob", "polyrob.exe")
    assert words[2:] == ["-P", profile_name, "$@"]


def test_wrapper_script_content_and_collision(env, tmp_path):
    r = _run("create", "scout")  # default --alias
    assert r.exit_code == 0, r.output
    wrapper = tmp_path / "bin" / "scout"
    text = wrapper.read_text()
    _assert_wrapper_command(text, "scout")
    assert "polyrob-profile-wrapper: scout" in text
    # collision with a foreign file refuses
    foreign = tmp_path / "bin" / "mytool"
    foreign.write_text("#!/bin/sh\necho mine\n")
    r = CliRunner().invoke(profile, ["alias", "scout", "--as", "mytool"])
    assert r.exit_code != 0 and "refusing" in r.output
    assert foreign.read_text() == "#!/bin/sh\necho mine\n"


def test_rename_updates_sticky_and_wrapper(env, tmp_path):
    _run("create", "old")
    _run("use", "old")
    r = _run("rename", "old", "new")
    assert r.exit_code == 0, r.output
    assert (env / "profiles" / "new").is_dir()
    assert not (env / "profiles" / "old").exists()
    assert (env / "active_profile").read_text().strip() == "new"
    assert not (tmp_path / "bin" / "old").exists()
    _assert_wrapper_command((tmp_path / "bin" / "new").read_text(), "new")
    assert "POLYROB_INSTANCE_ID=new" in (env / "profiles" / "new" / ".env").read_text()


def test_adopt_creates_profile_and_pin(env, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".polyrob" / "identity" / "polyrob" / "user_x").mkdir(parents=True)
    (proj / ".polyrob" / "identity" / "polyrob" / "user_x" / "self.md").write_text("S\n")
    monkeypatch.chdir(proj)
    r = _run("adopt", "myproj")
    assert r.exit_code == 0, r.output
    assert (proj / ".polyrob" / "profile").read_text().strip() == "myproj"
    assert (env / "profiles" / "myproj" / "data" / "identity" / "polyrob"
            / "user_x" / "self.md").is_file()
    # the pin now selects the profile from inside the folder
    from core.profiles import resolve_active_profile
    sel = resolve_active_profile()
    assert sel is not None and (sel.name, sel.source) == ("myproj", "project_pin")


def test_use_clear_removes_sticky(env):
    _run("create", "scout", "--no-alias")
    _run("use", "scout")
    r = _run("use", "--clear")
    assert r.exit_code == 0
    assert not (env / "active_profile").exists()


def test_group_level_dash_P_applies_before_any_subcommand(env):
    """W7: every subcommand (telegram/email/serve/…) inherits -P for free —
    the GROUP callback applies the profile env before the subcommand runs.
    Proven through the real group with a subcommand that reads the env."""
    from cli.polyrob import cli
    _run("create", "scout", "--no-alias")
    r = CliRunner().invoke(cli, ["-P", "scout", "profile", "path"],
                           catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert r.output.strip().endswith(str(Path("profiles") / "scout"))


def test_create_service_emits_unit_with_explicit_homes(env, tmp_path, monkeypatch):
    r = _run("create", "daemon1", "--no-alias", "--service")
    assert r.exit_code == 0, r.output
    unit = env / "profiles" / "daemon1" / "polyrob@.service"
    if not unit.exists():  # root could write /etc — never true in CI/dev
        return
    text = unit.read_text()
    assert 'POLYROB_PROFILE=%i' in text          # a systemd TEMPLATE unit
    assert f'POLYROB_PROFILES_ROOT={env / "profiles"}' in text
    assert "polyrob@daemon1" in r.output and "polyrob-daemon1" not in r.output
