"""The Docker sandbox way out of the custody file gap (field report 2026-10-29).

A managed user-mode install (``systemctl --user`` unit, seed in ~/.polyrob/.env)
needed: CODE_EXEC_ENABLED=true, CODE_EXEC_BACKEND=docker, CODE_EXEC_NETWORK=proxy,
the agent user in the ``docker`` group, and a restart of ``user@<uid>.service``
(``systemctl --user restart`` keeps the old groups). The custody refusal and
``polyrob doctor`` print exactly these steps, with the uid/user filled in.
"""
import grp
import os
import pwd
import types

import pytest

import core.security.custody_env as ce
import core.security.host_execution as he
import core.security.process_hardening as ph
import core.security.sandbox_setup as ss
from core.config_policy.members import is_member
from core.security import settings as st

SEED = "test-only-not-a-real-seed-" * 3


def test_the_env_lines_are_direct_lines_not_posture_members():
    for name, _ in ss.DOCKER_SANDBOX_ENV:
        assert not is_member(name), f"{name} is a posture member: print POLYROB_OVERRIDES"
    assert ss.docker_sandbox_env_lines() == [
        "CODE_EXEC_ENABLED=true", "CODE_EXEC_BACKEND=docker", "CODE_EXEC_NETWORK=proxy"]


def test_the_steps_fill_in_uid_and_user_and_name_the_manager_restart():
    steps = ss.docker_sandbox_steps(uid=1001, user="polyrob")
    for part in ("CODE_EXEC_ENABLED=true", "CODE_EXEC_BACKEND=docker",
                 "CODE_EXEC_NETWORK=proxy", "`usermod -aG docker polyrob`",
                 "`systemctl restart user@1001.service`",
                 "a `systemctl --user restart` keeps the old groups", "`polyrob doctor`"):
        assert part in steps


def test_root_prints_placeholders_not_its_own_uid():
    steps = ss.docker_sandbox_steps(uid=0, user="root")
    assert "user@<uid>.service" in steps and "usermod -aG docker <agent-user>" in steps


# -- the socket check ---------------------------------------------------------

@pytest.fixture
def sock(tmp_path):
    path = tmp_path / "docker.sock"
    path.write_text("")
    path.chmod(0o660)
    return path


def _other_gid():
    mine = set(os.getgroups()) | {os.getegid()}
    for g in grp.getgrall():
        if g.gr_gid not in mine:
            return g.gr_gid
    pytest.skip("no group outside this process's groups")


def test_a_socket_in_a_held_group_is_reachable(sock):
    me = os.getuid()
    acc = ss.socket_access(str(sock), uid=me + 1 if me else 12345, user="nobody-test",
                           groups={os.stat(sock).st_gid})
    assert acc.exists and acc.reachable and acc.in_process_groups


#: A uid that is never this process (a CI runner IS uid 1001 and owns the socket).
_UID = 4242 if os.getuid() != 4242 else 4243


def test_a_group_added_after_login_is_named_with_the_manager_restart(sock, monkeypatch):
    gid = os.stat(sock).st_gid
    monkeypatch.setattr(ss, "_user_in_group", lambda user, g: True)
    acc = ss.socket_access(str(sock), uid=_UID, user="polyrob", groups=set())
    assert acc.exists and not acc.reachable and acc.user_in_group
    hint = ss.stale_group_hint(acc, uid=_UID)
    assert hint and "started before that" in hint
    assert f"`systemctl restart user@{_UID}.service`" in hint
    assert str(acc.group or gid) in hint


def test_no_stale_hint_when_the_user_is_not_in_the_group(sock, monkeypatch):
    monkeypatch.setattr(ss, "_user_in_group", lambda user, g: False)
    acc = ss.socket_access(str(sock), uid=_UID, user="polyrob", groups=set())
    assert not acc.reachable and ss.stale_group_hint(acc, uid=_UID) is None


def test_a_missing_socket_says_so(tmp_path):
    acc = ss.socket_access(str(tmp_path / "none.sock"))
    assert not acc.exists and not acc.reachable


def test_a_tcp_docker_host_is_not_probed():
    assert ss.docker_socket_path({"DOCKER_HOST": "tcp://10.0.0.1:2375"}) is None
    assert ss.docker_socket_path({"DOCKER_HOST": "unix:///run/d.sock"}) == "/run/d.sock"
    assert ss.docker_socket_path({}) == ss.DEFAULT_DOCKER_SOCK


# -- the custody refusal carries the steps ------------------------------------

@pytest.fixture
def custody(monkeypatch, tmp_path):
    for name in (*ce.CUSTODY_SECRET_ENV, st.ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("WALLET_SIGNER", "local")
    ce._reset_for_tests()
    monkeypatch.setattr(ph, "hardening_state",
                        lambda: ph.HardeningResult(ph.STATE_HELD, "test: held"))
    path = tmp_path / ".env"
    path.write_text(f"AGENT_WALLET_MASTER_SEED={SEED}\n")
    path.chmod(0o600)
    ce.note_env_file(path, {"AGENT_WALLET_MASTER_SEED": SEED})
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    ce.take_custody_secrets()
    yield tmp_path
    ce._reset_for_tests()


def test_the_custody_refusal_prints_the_steps_for_this_user(custody):
    why = he.model_code_host_refusal()
    uid = os.getuid()
    user = pwd.getpwuid(uid).pw_name
    assert "Docker sandbox steps:" in why
    assert "CODE_EXEC_NETWORK=proxy" in why
    if uid != 0:
        assert f"usermod -aG docker {user}" in why
        assert f"systemctl restart user@{uid}.service" in why
    assert SEED not in why


# -- polyrob doctor -----------------------------------------------------------

def test_doctor_names_missing_settings_and_the_steps_under_custody(custody, monkeypatch, sock):
    from cli.commands import doctor_sandbox as ds
    env = {"DOCKER_HOST": f"unix://{sock}", "CODE_EXEC_BACKEND": "docker"}
    lines = ds.docker_sandbox_lines(env)
    text = "\n".join(lines)
    assert lines[0].startswith("docker socket:")
    assert "custody sandbox: !" in text
    assert "CODE_EXEC_ENABLED=true" in text.split("Not set yet:")[1].split(". Steps")[0]
    assert "CODE_EXEC_BACKEND=docker" not in text.split("Not set yet:")[1].split(". Steps")[0]
    assert "Steps: (1)" in text


def test_doctor_is_quiet_about_custody_without_a_file_seed(monkeypatch, sock):
    from cli.commands import doctor_sandbox as ds
    monkeypatch.setattr(he, "custody_file_gap", lambda: None)
    lines = ds.docker_sandbox_lines({"DOCKER_HOST": f"unix://{sock}"})
    assert len(lines) >= 1 and not any(l.startswith("custody sandbox") for l in lines)


def test_doctor_reads_the_user_managers_groups(tmp_path, monkeypatch, sock):
    """doctor's own ssh login holds the new group; the user manager does not."""
    from cli.commands import doctor_sandbox as ds
    gid = os.stat(sock).st_gid
    uid = os.getuid()
    if uid == 0:
        pytest.skip("root reaches every socket")
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    (proc / "4242" / "status").write_text(
        f"Name:\tsystemd\nPPid:\t1\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        f"Gid:\t{gid + 1}\t{gid + 1}\t{gid + 1}\t{gid + 1}\nGroups:\t{gid + 2}\n")
    monkeypatch.setattr(ds, "_PROC", proc)
    acc = ss.SocketAccess(path=str(sock), exists=True, reachable=True, gid=gid,
                          group="docker", in_process_groups=True, user_in_group=True)
    monkeypatch.setattr(ss, "socket_access", lambda path, **k: acc)
    lines, ok = ds._socket_lines({"DOCKER_HOST": f"unix://{sock}"}, proc)
    assert not ok
    assert f"user@{uid}.service (pid 4242) does not hold group docker" in lines[1]
    assert f"`systemctl restart user@{uid}.service`" in lines[1]
    # the manager holds it -> no second line
    (proc / "4242" / "status").write_text(
        f"Name:\tsystemd\nPPid:\t1\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t1\t1\t1\t1\n"
        f"Groups:\t{gid}\n")
    lines, ok = ds._socket_lines({"DOCKER_HOST": f"unix://{sock}"}, proc)
    assert ok and len(lines) == 1


def test_doctor_registers_the_sandbox_checker():
    from cli.commands.doctor_host import _checkers
    from cli.commands.doctor_sandbox import docker_sandbox_lines
    assert docker_sandbox_lines in _checkers()


def test_the_sandbox_guard_names_the_restart_for_a_stale_group(monkeypatch, sock):
    from tools.code_exec import sandbox_guard
    from tools.code_exec.backends import docker as dk
    monkeypatch.setattr(dk, "docker_binary_is_daemonless", lambda: False)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{sock}")
    monkeypatch.setattr(sandbox_guard.os, "access", lambda p, m: False)
    acc = ss.SocketAccess(path=str(sock), exists=True, reachable=False, gid=1,
                          group="docker", in_process_groups=False, user_in_group=True)
    monkeypatch.setattr(ss, "socket_access", lambda path, **k: acc)
    why = sandbox_guard.docker_socket_unreachable_reason("docker")
    assert "started before that" in why and "user@" in why
    assert "proposal 053" not in why


def test_docker_sandbox_env_has_no_posture_member():
    # A posture member is read only from POLYROB_OVERRIDES; a direct env line
    # for one would be ignored, so the printed steps would not work.
    from core.config_policy.members import MEMBER_FLAGS
    from core.security.sandbox_setup import DOCKER_SANDBOX_ENV
    assert not {name for name, _ in DOCKER_SANDBOX_ENV} & set(MEMBER_FLAGS)
