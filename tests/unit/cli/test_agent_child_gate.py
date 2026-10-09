"""EXEC-1/SUP-10 residual: the owner's admin CLI refuses in a process the agent started.

The marker rides every agent child env (``build_child_env``, the MCP child env,
the docker sandbox flags); ``cli.polyrob.main`` refuses owner verbs under it and
keeps the read verbs open.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli import agent_child_gate as gate
from core.security import agent_child
from core.security.agent_child import AGENT_CHILD_ENV

REPO = Path(__file__).resolve().parents[3]

OWNER_VERBS = [
    "", "wallet export", "wallet set-cap 5", "wallet nft transfer", "owner pair approve x",
    "owner allow telegram 1", "owner groups allow telegram 1", "config set A 1",
    "config unset A", "approvals add x", "run hi", "chat", "goals create x",
    "cron schedule x", "model", "update", "telegram", "keys create", "pack install x",
    "service install", "profile export", "init", "uninstall",
    "run -m --help x", "config set K -- --help",
]
READ_VERBS = [
    "doctor", "version", "--help", "-V", "wallet", "wallet --json", "wallet export --help",
    "owner pair pending", "owner groups list", "config get A", "config show",
    "approvals list", "goals list", "model list", "session costs abc", "skills list",
    "tools list", "owner memory scopes list", "rails", "service", "finance", "owner",
    "auth list",
]


@pytest.mark.parametrize("argv", OWNER_VERBS)
def test_owner_verbs_are_not_reads(argv):
    assert gate.is_read_invocation(argv.split()) is False


@pytest.mark.parametrize("argv", READ_VERBS)
def test_read_verbs_stay_open(argv):
    assert gate.is_read_invocation(argv.split()) is True


def test_refusal_only_under_the_marker(tmp_path):
    no_proc = str(tmp_path / "noproc")
    assert gate.refusal(["wallet", "export"], environ={}, proc_root=no_proc) is None
    msg = gate.refusal(["wallet", "export"], environ={AGENT_CHILD_ENV: "1"}, proc_root=no_proc)
    assert msg and "owner-only" in msg and AGENT_CHILD_ENV in msg
    assert gate.refusal(["doctor"], environ={AGENT_CHILD_ENV: "1"}, proc_root=no_proc) is None


def _fake_proc(root: Path, pid: int, ppid: int, env: dict) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "stat").write_bytes(f"{pid} (b a) (sh)) S {ppid} 1 1".encode())
    (d / "environ").write_bytes(b"".join(f"{k}={v}\0".encode() for k, v in env.items()))


def test_ancestor_marker_survives_an_unset_in_the_grandchild(tmp_path):
    # 300 = the agent's bash (exec-time env marked); 400 = `env -u` child; 500 = polyrob.
    _fake_proc(tmp_path, 300, 200, {"PATH": "/bin", AGENT_CHILD_ENV: "1"})
    _fake_proc(tmp_path, 400, 300, {"PATH": "/bin"})
    _fake_proc(tmp_path, 500, 400, {"PATH": "/bin"})
    _fake_proc(tmp_path, 200, 1, {"PATH": "/bin"})
    assert agent_child.is_agent_child({}, pid=500, proc_root=str(tmp_path)) is True
    assert agent_child.is_agent_child({}, pid=200, proc_root=str(tmp_path)) is False


def test_marker_value_zero_is_not_a_mark(tmp_path):
    assert agent_child.is_agent_child({AGENT_CHILD_ENV: "0"},
                                      proc_root=str(tmp_path / "x")) is False


def test_every_agent_child_env_carries_the_marker():
    from tools.code_exec.env_policy import build_child_env
    from tools.mcp.child_env import build_mcp_child_env
    assert build_child_env({})[AGENT_CHILD_ENV] == "1"
    assert build_child_env({AGENT_CHILD_ENV: "0"})[AGENT_CHILD_ENV] == "1"
    assert build_mcp_child_env({AGENT_CHILD_ENV: "0"})[AGENT_CHILD_ENV] == "1"
    from tools.shell.host_executor import host_child_env
    assert host_child_env()[AGENT_CHILD_ENV] == "1"


def _run_cli(args, marked: bool):
    env = {k: v for k, v in os.environ.items() if k != AGENT_CHILD_ENV}
    if marked:
        env[AGENT_CHILD_ENV] = "1"
    return subprocess.run(
        [sys.executable, "-c", "import sys; from cli.polyrob import main; sys.argv[0]='polyrob'; main()",
         *args], cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)


def test_entry_point_refuses_wallet_export_for_an_agent_child():
    proc = _run_cli(["wallet", "export"], marked=True)
    assert proc.returncode == 1
    assert "owner-only command" in proc.stderr


def test_entry_point_keeps_version_for_an_agent_child():
    proc = _run_cli(["version"], marked=True)
    assert proc.returncode == 0, proc.stderr
    assert "polyrob v" in proc.stdout


def test_cli_runner_is_not_gated_so_the_repo_suite_runs_under_the_marker(monkeypatch):
    from click.testing import CliRunner

    from cli.polyrob import cli
    monkeypatch.setenv(AGENT_CHILD_ENV, "1")
    result = CliRunner().invoke(cli, ["version"])
    assert result.exit_code == 0
