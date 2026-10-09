"""`polyrob service` and `polyrob uninstall` (062).

The two behaviours worth pinning: a systemd USER unit inherits nothing from the
shell, so it must pin POLYROB_HOME itself; and the uninstaller must strip the
installer's PATH block without eating the lines around it.
"""
import sys

import pytest
from click.testing import CliRunner

from core.runtime_paths import _local_default_data_home as _REAL_LOCAL_DATA_HOME


@pytest.fixture(autouse=True)
def _hermetic_uninstall(tmp_path, monkeypatch):
    """CLI16 (2026-10-03): every test here is hermetic, whatever ran before it.

    `uninstall_cmd` reached four pieces of process-wide state: the REAL home
    (`remove_path_blocks(Path.home())` rewrites the shell rc files, and
    `service_uninstall` reads — and on a hit unloads — the launchd plist / systemd
    unit under it), the once-per-process env ladder (`ensure_env_loaded`, which
    loads whatever `.env` the process first saw and can move POLYROB_HOME through
    an active profile), the CWD, and the install probe. Which of those a test met
    depended on the tests that ran before it, so `test_purge_names_both_homes…`
    passed alone and failed in the `-k cli` run. Each is pinned per test.
    """
    home = tmp_path / "_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cli.commands._bootstrap._env_loaded", True)
    monkeypatch.setattr("cli.commands.uninstall._package_removal_hint",
                        lambda: "remove the virtualenv you installed into")


def test_exec_argv_uses_this_interpreter_and_the_module(monkeypatch):
    from cli.commands import service

    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    argv = service._exec_argv()
    assert argv[0] == sys.executable, (
        "a bare `polyrob` on PATH can be a stale wheel — run the module with "
        "the interpreter the user installed into")
    assert argv[1:] == ["-I", "-m", "cli.polyrob", "gateway"]


def test_exec_argv_carries_the_active_profile(monkeypatch):
    from cli.commands import service

    monkeypatch.setenv("POLYROB_PROFILE", "scout")
    assert service._exec_argv()[:6] == [sys.executable, "-I", "-m", "cli.polyrob", "-P", "scout"]


@pytest.mark.skipif(sys.platform == "win32", reason="posix homes")
def test_systemd_unit_pins_the_home(tmp_path, monkeypatch):
    from cli.commands import service

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    unit = service._write_systemd_unit()
    body = unit.read_text()
    assert "Environment=\"POLYROB_HOME=" in body, (
        "a systemd --user unit exports no shell env; an implicit home starts "
        "the agent keyless")
    assert "ExecStart=" in body and "cli.polyrob" in body
    assert "WantedBy=default.target" in body


def test_service_needed_exits_nonzero_without_a_surface(monkeypatch):
    from cli.commands.service import service as service_group

    for var in ("TELEGRAM_SURFACE_ENABLED", "DISCORD_SURFACE_ENABLED",
                "SLACK_SURFACE_ENABLED", "EMAIL_SURFACE_ENABLED",
                "SIGNAL_SURFACE_ENABLED", "X_SURFACE_ENABLED",
                "WHATSAPP_SURFACE_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    res = CliRunner().invoke(service_group, ["needed"])
    assert res.exit_code != 0


def test_service_needed_names_the_surface(monkeypatch):
    from cli.commands.service import service as service_group

    monkeypatch.setenv("TELEGRAM_SURFACE_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    res = CliRunner().invoke(service_group, ["needed"])
    assert res.exit_code == 0
    assert "telegram" in res.output


def test_remove_path_blocks_leaves_the_neighbours(tmp_path):
    from cli.commands.uninstall import remove_path_blocks

    rc = tmp_path / ".zshrc"
    rc.write_text(
        'export KEEP_ME=1\n'
        '# >>> polyrob >>>\n'
        'export PATH="$HOME/.local/bin:$PATH"\n'
        '# <<< polyrob <<<\n'
        'export ALSO_KEEP=2\n')
    touched = remove_path_blocks(tmp_path)
    assert touched == [rc]
    body = rc.read_text()
    assert "export KEEP_ME=1" in body and "export ALSO_KEEP=2" in body
    assert "polyrob" not in body


def test_remove_path_blocks_is_a_noop_without_the_markers(tmp_path):
    from cli.commands.uninstall import remove_path_blocks

    rc = tmp_path / ".bashrc"
    rc.write_text("export PATH=/usr/bin\n")
    assert remove_path_blocks(tmp_path) == []
    assert rc.read_text() == "export PATH=/usr/bin\n"


def test_find_shim_ignores_a_foreign_polyrob_on_path(tmp_path, monkeypatch):
    from cli.commands import uninstall as un

    fake_home = tmp_path
    (fake_home / ".local" / "bin").mkdir(parents=True)
    other = fake_home / ".local" / "bin" / "polyrob"
    other.write_text("#!/bin/sh\necho not ours\n")
    monkeypatch.setattr(un.Path, "home", staticmethod(lambda: fake_home))
    assert un.find_shim() is None, "only OUR launcher (it runs cli.polyrob) is removed"
    other.write_text('#!/usr/bin/env bash\nexec python -m cli.polyrob "$@"\n')
    assert un.find_shim() == other


def test_purge_names_both_homes_and_never_only_the_cwd_one(tmp_path, monkeypatch):
    """A data home set apart from the config home (POLYROB_DATA_DIR) is named
    next to the config home — the keys and the wallet seed are never left out."""
    from cli.commands.uninstall import uninstall_cmd

    config = tmp_path / "config"
    project = tmp_path / "project"
    (config / "wallet").mkdir(parents=True)
    (project / ".polyrob").mkdir(parents=True)
    monkeypatch.setenv("POLYROB_HOME", str(config))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(project / ".polyrob"))
    monkeypatch.setattr("cli.commands.uninstall.find_shim", lambda: None)

    res = CliRunner().invoke(uninstall_cmd, [])
    assert res.exit_code == 0, res.output
    assert str(config) in res.output
    assert str(project / ".polyrob") in res.output
    assert "per-project" not in res.output


def test_default_data_home_is_inside_config_home_and_cwd_polyrob_is_only_named(
        tmp_path, monkeypatch):
    """The default data home is <config>/data; a legacy cwd/.polyrob is named
    as not loaded and never deleted."""
    from cli.commands.uninstall import uninstall_cmd

    config = tmp_path / "config"
    proj = tmp_path / "proj"
    (config / "data").mkdir(parents=True)
    (proj / ".polyrob").mkdir(parents=True)
    monkeypatch.setenv("POLYROB_HOME", str(config))
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.chdir(proj)
    # the REAL local default (conftest points it at the cwd for isolation)
    import core.runtime_paths as _rp
    monkeypatch.setattr(_rp, "_local_default_data_home", _REAL_LOCAL_DATA_HOME)
    monkeypatch.setattr("cli.commands.uninstall.find_shim", lambda: None)

    res = CliRunner().invoke(uninstall_cmd, ["--purge"], input="DELETE\n")
    assert res.exit_code == 0, res.output
    assert "inside the config home" in res.output
    assert "no longer loads it" in res.output
    assert "Could not delete" not in res.output
    assert not config.exists()
    assert (proj / ".polyrob").is_dir(), "a legacy project folder is never deleted"


def test_purge_deletes_both_homes_after_confirmation(tmp_path, monkeypatch):
    from cli.commands.uninstall import uninstall_cmd

    config = tmp_path / "config"
    data = tmp_path / "project" / ".polyrob"
    config.mkdir(parents=True)
    data.mkdir(parents=True)
    monkeypatch.setenv("POLYROB_HOME", str(config))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data))
    monkeypatch.setattr("cli.commands.uninstall.find_shim", lambda: None)

    res = CliRunner().invoke(uninstall_cmd, ["--purge"], input="DELETE\n")
    assert res.exit_code == 0, res.output
    assert not config.exists() and not data.exists()
    assert "wallet seed" in res.output


def test_purge_cancels_on_anything_but_the_typed_word(tmp_path, monkeypatch):
    from cli.commands.uninstall import uninstall_cmd

    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("POLYROB_HOME", str(config))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(config))
    monkeypatch.setattr("cli.commands.uninstall.find_shim", lambda: None)

    res = CliRunner().invoke(uninstall_cmd, ["--purge"], input="yes\n")
    assert res.exit_code == 0
    assert config.exists(), "a destructive verb takes the exact word or nothing"
    assert "Cancelled" in res.output


def test_doctor_service_line_sees_a_system_unit(monkeypatch, tmp_path):
    """⚠️ A DEPLOYED box runs polyrob* SYSTEM units and owns no user unit. The
    line used to read "not installed" there — while polyrob.service was the
    process asking the question."""
    from cli.commands import doctor as doc

    monkeypatch.setattr("cli.commands.service._systemd_unit_path",
                        lambda: tmp_path / "absent.service")
    monkeypatch.setattr("cli.commands.service._launchd_plist_path",
                        lambda: tmp_path / "absent.plist")
    monkeypatch.setattr("cli.commands.update._detect_polyrob_units",
                        lambda: ["polyrob.service", "polyrob-email.service"])
    line = doc._service_line()
    assert "polyrob.service" in line and "system" in line
    assert "not installed" not in line


def test_doctor_service_line_says_not_installed_when_nothing_exists(monkeypatch, tmp_path):
    from cli.commands import doctor as doc

    monkeypatch.setattr("cli.commands.service._systemd_unit_path",
                        lambda: tmp_path / "absent.service")
    monkeypatch.setattr("cli.commands.service._launchd_plist_path",
                        lambda: tmp_path / "absent.plist")
    monkeypatch.setattr("cli.commands.update._detect_polyrob_units", lambda: [])
    assert "not installed" in doc._service_line()


def test_uninstall_never_touches_the_real_home(tmp_path, monkeypatch):
    """CLI16: the PATH block is stripped from the HERMETIC home, not the user's."""
    from pathlib import Path
    from cli.commands.uninstall import uninstall_cmd

    assert Path.home() == tmp_path / "_home"
    rc = Path.home() / ".zshrc"
    rc.write_text("keep\n# >>> polyrob >>>\nexport PATH=x\n# <<< polyrob <<<\n")
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("cli.commands.uninstall.find_shim", lambda: None)
    res = CliRunner().invoke(uninstall_cmd, [])
    assert res.exit_code == 0, res.output
