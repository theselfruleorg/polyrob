"""080 WS-1 item 1 — one instance env file.

On a box with a deployed instance every process (agent, console, email,
gateway, a root CLI) resolves the SAME env file: ``<deployed data home>/.env``.
A plain local CLI (nothing deployed) keeps ``$POLYROB_HOME/.env``.
"""
from pathlib import Path

import pytest


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    """A fake deployed box: an env file that declares the data home, no shell env."""
    data_home = tmp_path / "var-lib-polyrob"
    data_home.mkdir()
    env_file = tmp_path / "etc-polyrob.env"
    env_file.write_text(f"POLYROB_DATA_DIR={data_home}\nDEFAULT_MODEL=sys-model\n")
    import core.admin_data_home as adh
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", str(env_file))
    monkeypatch.setattr(adh, "_deployment_evidence", lambda: [str(env_file)])
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "user-home"))
    return data_home


def test_local_cli_keeps_the_user_home_file(tmp_path, monkeypatch):
    import core.admin_data_home as adh
    monkeypatch.setattr(adh, "_deployment_evidence", lambda: [])
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "h"))
    from core.config_service import home_env_path
    from core.runtime_paths import instance_env_path
    assert instance_env_path() is None
    assert home_env_path() == tmp_path / "h" / ".env"


def test_root_cli_on_the_box_adopts_the_deployed_home(deployed):
    from core.config_service import home_env_path
    assert home_env_path() == deployed.resolve() / ".env"


def test_service_process_with_data_dir_uses_the_same_file(deployed, monkeypatch, tmp_path):
    # A service (console, email, agent) has POLYROB_DATA_DIR from systemd and
    # its OWN $HOME — the file must not depend on $HOME.
    monkeypatch.setenv("POLYROB_DATA_DIR", str(deployed))
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "console-home"))
    from core.config_service import home_env_path
    assert home_env_path() == deployed.resolve() / ".env"


def test_instance_file_is_on_the_load_ladder_first(deployed, monkeypatch):
    from core.paths import env_file_candidates
    cands = env_file_candidates()
    assert cands[0].tier == "instance"
    assert cands[0].path == deployed.resolve() / ".env"


def test_load_env_reads_the_instance_file(deployed, monkeypatch):
    (deployed / ".env").write_text("POLYROB_PROBE_080=instance\n")
    monkeypatch.delenv("POLYROB_PROBE_080", raising=False)
    from polyrob.app.bootstrap import load_env
    load_env("development")
    import os
    assert os.environ.get("POLYROB_PROBE_080") == "instance"
    monkeypatch.delenv("POLYROB_PROBE_080", raising=False)


def test_unreadable_deployed_file_falls_back_to_user_home(tmp_path, monkeypatch):
    import core.admin_data_home as adh

    def _boom(key):
        raise adh.DeployedEnvUnreadable("x")
    monkeypatch.setattr(adh, "_deployment_evidence", lambda: ["/etc/polyrob/polyrob.env"])
    monkeypatch.setattr(adh, "deployed_env_value", _boom)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "h"))
    from core.config_service import home_env_path
    assert home_env_path() == tmp_path / "h" / ".env"


def test_a_write_to_the_instance_file_is_group_readable(deployed, monkeypatch):
    """Every service identity of the instance (group polyrob-data) must read it."""
    import os
    import stat
    monkeypatch.setenv("POLYROB_DATA_DIR", str(deployed))
    from core.config_service import set_value
    res = set_value("GOALS_ENABLED", "true", surface="local")
    assert res.ok, res.message
    mode = stat.S_IMODE(os.stat(deployed / ".env").st_mode)
    assert mode == 0o640


def test_an_unreadable_ladder_file_does_not_crash_load_env(deployed, monkeypatch):
    import dotenv

    def _denied(*a, **k):
        raise PermissionError("denied")
    import polyrob.app.bootstrap as boot
    (deployed / ".env").write_text("X=1\n")
    monkeypatch.setattr(boot, "load_dotenv", _denied)
    boot.load_env("development")  # must not raise
