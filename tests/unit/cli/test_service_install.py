"""098 CL-10: `service install` checks every systemctl step; `service status`
shows enabled/disabled; 098 CO-02: `service stop` withdraws an unstarted request."""
import pytest
from click.testing import CliRunner

from core import lifecycle as lc


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    home = tmp_path / "_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "ph"))
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    monkeypatch.setattr("cli.commands._bootstrap._env_loaded", True)
    monkeypatch.setattr("cli.commands.service._data_home", lambda: tmp_path / "data")


class _Res:
    def __init__(self, out="", rc=0, err=""):
        self.stdout, self.stderr, self.returncode = out, err, rc


@pytest.fixture
def linux(monkeypatch):
    from cli.commands import service
    monkeypatch.setattr(service, "_platform", lambda: "linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("cli.surfaces_config.gateway_surfaces", lambda env: ["telegram"])
    return service


def test_enable_failure_is_reported(linux, monkeypatch):
    calls = []

    def _run(cmd, check=False):
        calls.append(cmd)
        if "enable" in cmd:
            return _Res(rc=1, err="Failed to connect to user scope bus")
        return _Res()

    monkeypatch.setattr(linux, "_run", _run)
    res = CliRunner().invoke(linux.service, ["install", "--no-start"])
    assert res.exit_code != 0
    assert "enable" in res.output and "no user bus" in res.output
    assert "Enabled" not in res.output


def test_status_shows_enabled_flag(linux, monkeypatch):
    linux._write_systemd_unit()

    def _run(cmd, check=False):
        if "is-active" in cmd:
            return _Res("active")
        if "is-enabled" in cmd:
            return _Res("disabled")
        return _Res()

    monkeypatch.setattr(linux, "_run", _run)
    res = CliRunner().invoke(linux.service, ["status"])
    assert "service: active, disabled" in res.output


def test_service_stop_withdraws_an_unstarted_update(linux, monkeypatch, tmp_path):
    linux._write_systemd_unit()
    home = tmp_path / "data"
    lc.request_restart(kind=lc.KIND_UPDATE, requested_by="owner", data_home=home)

    def _run(cmd, check=False):
        if "is-active" in cmd:
            return _Res("active")
        return _Res()

    monkeypatch.setattr(linux, "_run", _run)
    res = CliRunner().invoke(linux.service, ["stop"])
    assert res.exit_code == 0, res.output
    assert "withdrawn" in res.output
    assert lc.read_request(data_home=home) is None
    assert lc.history(limit=1, data_home=home)[0]["outcome"] == lc.OUTCOME_CANCELLED
