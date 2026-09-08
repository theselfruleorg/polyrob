"""031: `polyrob autonomy` / `polyrob owner` act on the DEPLOYED data home.

The production shape: the daemon gets `POLYROB_DATA_DIR=/var/lib/polyrob` from
systemd's `EnvironmentFile=/etc/polyrob/polyrob.env`; the owner's SSH shell does
not. `resolve_data_home()` then answers `cwd/.polyrob`, so `polyrob autonomy
pause` wrote the pause record somewhere the daemon never reads and still printed
a verified "⏸ Paused everything".
"""
import pytest
from click.testing import CliRunner


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A box with no deployment; both probes point at empty tmp locations."""
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.setattr("cli.commands._bootstrap._env_loaded", True)
    local = tmp_path / "shell_local"
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: local)
    monkeypatch.setattr("core.admin_data_home.DEPLOYED_ENV_FILE",
                        str(tmp_path / "nope" / "polyrob.env"))
    units = tmp_path / "units"
    units.mkdir()
    monkeypatch.setattr("core.admin_data_home.UNIT_DIRS", (str(units),))
    from core import admin_data_home as mod
    mod.reset_admin_data_home_notes()
    return tmp_path, local, units


def _deploy(box, monkeypatch, *, data_dir=None):
    tmp_path, _local, units = box
    envf = tmp_path / "etc" / "polyrob.env"
    envf.parent.mkdir(parents=True, exist_ok=True)
    envf.write_text(f"POLYROB_DATA_DIR={data_dir}\n" if data_dir else "TOKEN=x\n",
                    encoding="utf-8")
    monkeypatch.setattr("core.admin_data_home.DEPLOYED_ENV_FILE", str(envf))
    (units / "polyrob.service").write_text("[Unit]\n", encoding="utf-8")


def _autonomy():
    from cli.commands.autonomy import autonomy
    return CliRunner(), autonomy


def test_pause_writes_into_the_deployed_data_home(box, monkeypatch):
    tmp_path, local, _units = box
    deployed = tmp_path / "var_lib_polyrob"
    deployed.mkdir()
    _deploy(box, monkeypatch, data_dir=str(deployed))

    runner, autonomy = _autonomy()
    res = runner.invoke(autonomy, ["pause"])
    assert res.exit_code == 0, res.output
    assert (deployed / "AUTONOMY_PAUSE.json").exists(), \
        "the pause must land in the home the daemon reads"
    # (`state_bases` still writes every base it probes, so the shell-local copy
    # is there too — belt and braces. Before the fix ONLY that copy existed and
    # the daemon saw nothing.)
    assert "DEPLOYED data home" in res.output or str(deployed) in res.output, \
        "the owner must be told which home was acted on"


def test_deployment_without_a_readable_home_refuses_instead_of_lying(box, monkeypatch):
    tmp_path, local, _units = box
    _deploy(box, monkeypatch, data_dir=None)

    runner, autonomy = _autonomy()
    res = runner.invoke(autonomy, ["pause"])
    assert res.exit_code != 0, res.output
    assert "set -a" in res.output and str(local) in res.output
    assert not (local / "AUTONOMY_PAUSE.json").exists()


def test_owner_halt_shares_the_same_seam(box, monkeypatch):
    tmp_path, local, _units = box
    deployed = tmp_path / "var_lib_polyrob2"
    deployed.mkdir()
    _deploy(box, monkeypatch, data_dir=str(deployed))

    from cli.commands.owner import owner
    res = CliRunner().invoke(owner, ["halt"])
    assert res.exit_code == 0, res.output
    assert (deployed / "AUTONOMY_PAUSE.json").exists()


def test_local_only_box_is_unchanged(box):
    _tmp, local, _units = box
    runner, autonomy = _autonomy()
    res = runner.invoke(autonomy, ["pause"])
    assert res.exit_code == 0, res.output
    assert (local / "AUTONOMY_PAUSE.json").exists()
    assert "DEPLOYED data home" not in res.output, "no new noise on a dev box"
