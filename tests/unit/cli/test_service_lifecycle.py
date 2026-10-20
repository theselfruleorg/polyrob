"""077 — `polyrob service start|stop|restart|logs`, the user bus, root
delegation, and `update --apply` through a running service."""
import sys
import time
from pathlib import Path

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


# ---- the user bus -----------------------------------------------------------

def test_bus_env_fills_in_a_lingering_runtime_dir(tmp_path):
    from cli.commands._service_ops import user_bus_env
    rt = tmp_path / "run" / "1000"
    rt.mkdir(parents=True)
    (rt / "bus").touch()
    env = user_bus_env({}, uid=1000, runtime_root=str(tmp_path / "run"))
    assert env["XDG_RUNTIME_DIR"] == str(rt)
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={rt}/bus"


def test_bus_env_keeps_what_the_session_set(tmp_path):
    from cli.commands._service_ops import user_bus_env
    env = user_bus_env({"XDG_RUNTIME_DIR": "/mine"}, uid=1000,
                       runtime_root=str(tmp_path / "none"))
    assert env["XDG_RUNTIME_DIR"] == "/mine"


def test_bus_error_names_the_remedy():
    from cli.commands._service_ops import bus_error_hint
    msg = ("Failed to connect to user scope bus via local transport: "
           "$DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined")
    assert "polyrob service" in bus_error_hint(msg)
    assert bus_error_hint("Unit not found") == ""


# ---- root delegation -------------------------------------------------------

def _unit(tmp_path, user="polyrob"):
    home = tmp_path / "homes" / user
    unit = home / ".config" / "systemd" / "user" / "polyrob.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(
        "[Service]\n"
        'Environment="POLYROB_HOME=/home/polyrob/.polyrob"\n'
        "ExecStart=/home/polyrob/.polyrob/venv/bin/python -I -m cli.polyrob gateway\n")
    return home, unit


def test_service_users_finds_the_unit_owner(tmp_path):
    from cli.commands._service_ops import service_users
    home, unit = _unit(tmp_path)
    found = service_users(pw_entries=[("root", 0, str(tmp_path / "homes" / "root")),
                                      ("polyrob", 1000, str(home))])
    assert found == [("polyrob", 1000, unit)]


def test_delegation_runs_the_units_own_interpreter_as_that_user(tmp_path):
    from cli.commands._service_ops import delegation_argv
    _home, unit = _unit(tmp_path)
    argv = delegation_argv("polyrob", 1000, unit, ["restart", "--now"])
    assert argv[:4] == ["runuser", "-u", "polyrob", "--"]
    assert "XDG_RUNTIME_DIR=/run/user/1000" in argv
    assert "POLYROB_HOME=/home/polyrob/.polyrob" in argv
    i = argv.index("/home/polyrob/.polyrob/venv/bin/python")
    assert argv[i:] == ["/home/polyrob/.polyrob/venv/bin/python", "-I", "-m", "cli.polyrob",
                        "service", "restart", "--now"]
    assert delegation_argv("polyrob", 1000, unit, [], have_runuser=False)[0] == "sudo"


def test_verb_args_drop_the_user_option():
    from cli.commands._service_ops import verb_args_from_argv
    assert verb_args_from_argv(["polyrob", "service", "restart", "--user", "polyrob",
                                "--now"]) == ["restart", "--now"]
    assert verb_args_from_argv(["polyrob", "service", "--user=bob", "logs"]) == ["logs"]


# ---- the unit ----------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="posix homes")
def test_user_unit_carries_the_restart_contract():
    from cli.commands import service
    body = service._write_systemd_unit().read_text()
    for line in ("RestartForceExitStatus=75", "SuccessExitStatus=75",
                 "TimeoutStopSec=120",
                 "Restart=on-failure"):
        assert line in body, line


def test_launchd_plist_marks_the_supervisor():
    from cli.commands import service
    body = service._write_launchd_plist().read_text()
    from core.lifecycle import LAUNCHD_LABEL
    assert f"<key>Label</key><string>{LAUNCHD_LABEL}</string>" in body, \
        "core.lifecycle.supervisor() recognises launchd by this label"
    assert "<key>KeepAlive</key><true/>" in body


def test_deployment_units_carry_the_restart_contract():
    from cli.commands.profile import PROFILE_UNIT_TEMPLATE
    assert "RestartForceExitStatus=75" in PROFILE_UNIT_TEMPLATE
    # The deployment/ units' half lives in ops/tests/deployment (private tree).


# ---- restart -----------------------------------------------------------------

@pytest.fixture
def linux_service(monkeypatch, tmp_path):
    from cli.commands import service
    monkeypatch.setattr(service, "_platform", lambda: "linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/" + name)
    service._write_systemd_unit()
    calls = []

    class _Res:
        def __init__(self, out="", rc=0):
            self.stdout, self.stderr, self.returncode = out, "", rc

    state = {"active": True, "pid": 4242}

    def _run(cmd, check=False):
        calls.append(cmd)
        if "is-active" in cmd:
            return _Res("active" if state["active"] else "inactive")
        if "MainPID" in cmd:
            return _Res(str(state["pid"]))
        return _Res()

    monkeypatch.setattr(service, "_run", _run)
    return service, calls, state


def test_restart_asks_the_running_agent_and_reports(linux_service, tmp_path, monkeypatch):
    service, calls, state = linux_service
    home = tmp_path / "data"
    rec = lc.record_boot(data_home=home)
    state["pid"] = rec["pid"]

    def _wait(req_id, **kw):
        req = lc.read_request(data_home=home)
        assert req.id == req_id and req.origin == {"surface": "cli"}
        lc.close_request(lc.OUTCOME_BACK_ONLINE, "✅ Back online on 9.9.9 — restart took 3 s.",
                         data_home=home)
        return lc.history(limit=1, data_home=home)[0]

    monkeypatch.setattr("cli.commands._service_ops.wait_for_request", _wait)
    res = CliRunner().invoke(service.service, ["restart"])
    assert res.exit_code == 0, res.output
    assert "Back online on 9.9.9" in res.output
    assert not any("restart" in c for c in calls if c[:2] == ["systemctl", "--user"]), \
        "a drained restart never goes through systemctl restart"


def test_restart_falls_back_for_a_pre_077_process(linux_service, tmp_path, monkeypatch):
    service, calls, state = linux_service
    monkeypatch.setattr(service, "_report_boot", lambda *a, **k: None)
    res = CliRunner().invoke(service.service, ["restart"])  # no boot record at all
    assert res.exit_code == 0, res.output
    assert ["systemctl", "--user", "restart", "polyrob.service"] in calls


def test_restart_starts_a_stopped_service(linux_service, monkeypatch):
    service, calls, state = linux_service
    state["active"] = False
    monkeypatch.setattr(service, "_report_boot", lambda *a, **k: None)
    res = CliRunner().invoke(service.service, ["restart"])
    assert res.exit_code == 0, res.output
    assert ["systemctl", "--user", "start", "polyrob.service"] in calls


def test_restart_reports_a_called_off_drain(linux_service, tmp_path, monkeypatch):
    service, calls, state = linux_service
    home = tmp_path / "data"
    state["pid"] = lc.record_boot(data_home=home)["pid"]

    def _wait(req_id, **kw):
        lc.close_request(lc.OUTCOME_DRAIN_TIMEOUT, "1 cron job(s) running", data_home=home)
        return lc.history(limit=1, data_home=home)[0]

    monkeypatch.setattr("cli.commands._service_ops.wait_for_request", _wait)
    res = CliRunner().invoke(service.service, ["restart", "--no-wait"])
    assert res.exit_code != 0 and "cron job" in res.output


def test_wait_for_request_withdraws_an_unclaimed_request(tmp_path):
    from cli.commands._service_ops import wait_for_request
    home = tmp_path / "data"
    req, _ = lc.request_restart(requested_by="cli", data_home=home)
    t = {"now": 0.0}
    out = wait_for_request(req.id, data_home=home, timeout=100, poll=1,
                           clock=lambda: t["now"],
                           sleep=lambda s: t.__setitem__("now", t["now"] + s))
    assert out == {"outcome": "unclaimed"}
    assert lc.read_request(data_home=home) is None


def test_status_shows_running_version_and_waiting_settings(linux_service, tmp_path):
    service, calls, state = linux_service
    home = tmp_path / "data"
    lc.record_boot(data_home=home)
    time.sleep(0.01)
    lc.record_pending("TELEGRAM_ALLOWED_CHAT_IDS", data_home=home)
    lines = "\n".join(service.lifecycle_lines("linux"))
    assert "running:" in lines and "starting" in lines
    assert "TELEGRAM_ALLOWED_CHAT_IDS" in lines


def test_status_flags_a_unit_from_before_077(linux_service):
    service, calls, state = linux_service
    unit = service._systemd_unit_path()
    unit.write_text(unit.read_text().replace("RestartForceExitStatus=75\n", ""))
    assert any("polyrob service install" in l for l in service.lifecycle_lines("linux"))


# ---- update --apply through the service --------------------------------------

def test_update_apply_goes_through_a_running_service(monkeypatch, tmp_path):
    from cli.commands import update as upd
    from cli.update.versions import UpdateStatus

    home = tmp_path / "data"
    monkeypatch.setenv("INVOCATION_ID", "test-unit")
    boot = lc.record_boot(data_home=home)

    class _Ctx:
        method, repo_root = "editable_git", str(tmp_path)

    monkeypatch.setattr(upd, "_plan", lambda ch: (
        _Ctx(), UpdateStatus(current="1.3.0", latest="1.4.0", channel="stable"), None))
    monkeypatch.setattr(upd, "build_runners", lambda *a, **k: object())

    class _U:
        data_home = home

    monkeypatch.setattr(upd, "resolve_update_context", lambda: _U())
    monkeypatch.setattr(upd, "_running_service", lambda: boot)

    def _wait(req_id, **kw):
        req = lc.read_request(data_home=home)
        assert req.kind == lc.KIND_UPDATE and req.target_version == "1.4.0"
        lc.close_request(lc.OUTCOME_UPDATED, "✅ Updated and back online on 1.4.0",
                         data_home=home, version="1.4.0")
        return lc.history(limit=1, data_home=home)[0]

    monkeypatch.setattr("cli.commands._service_ops.wait_for_request", _wait)
    res = CliRunner().invoke(upd.update_cmd, ["--apply", "--yes"])
    assert res.exit_code == 0, res.output
    assert "Updated and back online on 1.4.0" in res.output
